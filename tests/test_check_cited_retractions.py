"""Known-answer tests for scripts/check_cited_retractions.py, against a fake local API."""
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import json
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlparse

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_cited_retractions.py"
SPEC = importlib.util.spec_from_file_location("check_cited_retractions", SCRIPT)
ccr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ccr)

FLAG = {"doi": "10.1/bad", "status": "retracted", "timing": "before"}


@pytest.fixture
def api():
    state = {"calls": [], "throttle": 0}

    class Fake(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, headers=None):
            self.send_response(code)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def do_GET(self):
            query = parse_qs(urlparse(self.path).query)["q"][0]
            state["calls"].append(query)
            if state["throttle"]:
                state["throttle"] -= 1
                return self._send(429, {"ok": False}, {"Retry-After": "1"})
            if query == "10.1/boom":
                return self._send(500, {"ok": False})
            if query in ("10.1/flagged", "111"):
                return self._send(200, {"ok": True, "refs_total": 3, "refs_checked": 2,
                                        "paper": {"own": {"status": "retracted"}}, "flagged": [FLAG]})
            if query == "10.1/clean":
                return self._send(200, {"ok": True, "refs_total": 3, "refs_checked": 3, "paper": {}, "flagged": []})
            return self._send(404, {"ok": False})

    server = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{server.server_port}/api/check"
    yield state
    server.shutdown()


def artifact(folder: Path, **paper) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "artifact.json"
    path.write_text(json.dumps({"paper": {"paper_id": "p", "title": "SECRET TITLE", **paper}, "claims": [{"id": "c1"}]}))
    return path


def run(api, *args) -> int:
    return ccr.main([*map(str, args), "--api", api["url"], "--delay", "0"])


def test_write_adds_only_the_side_file(api, tmp_path):
    path = artifact(tmp_path / "a", doi="10.1/flagged")
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    assert run(api, path, "--write") == 0

    assert sorted(p.name for p in path.parent.iterdir()) == ["artifact.json", "cited_retractions.json"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    record = json.loads((path.parent / "cited_retractions.json").read_text())
    assert record["flagged"] == [FLAG]
    assert record["refs_checked"] == 2
    assert record["paper_own_notice"] == {"status": "retracted"}


def test_default_only_prints(api, tmp_path, capsys):
    path = artifact(tmp_path / "a", doi="10.1/flagged")

    assert run(api, path) == 0

    assert not (path.parent / "cited_retractions.json").exists()
    assert "retracted: 10.1/bad" in capsys.readouterr().out


def test_clean_paper_writes_an_empty_list(api, tmp_path):
    path = artifact(tmp_path / "a", doi="10.1/clean")

    assert run(api, path, "--write") == 0

    assert json.loads((path.parent / "cited_retractions.json").read_text())["flagged"] == []


def test_only_identifiers_leave_the_machine_and_doi_falls_back_to_pmid(api, tmp_path):
    path = artifact(tmp_path / "a", doi="10.1/none", pmid="111")

    assert run(api, path, "--write") == 0

    assert api["calls"] == ["10.1/none", "111"]


def test_paper_with_no_identifier_sends_nothing(api, tmp_path):
    path = artifact(tmp_path / "a")

    assert run(api, path, "--write") == 0

    assert api["calls"] == []
    assert not (path.parent / "cited_retractions.json").exists()


def test_rate_limit_waits_once_then_succeeds(api):
    api["throttle"] = 1
    waits = []

    record = ccr.check({"doi": "10.1/clean"}, api["url"], sleep=waits.append)

    assert record["flagged"] == []
    assert waits == [1]


def test_rate_limited_twice_is_an_error_not_a_clean_result(api, tmp_path, monkeypatch):
    api["throttle"] = 2
    monkeypatch.setattr(ccr.time, "sleep", lambda _: None)
    path = artifact(tmp_path / "a", doi="10.1/clean")

    assert run(api, path, "--write") == 1

    assert not (path.parent / "cited_retractions.json").exists()


def test_server_error_is_an_error_and_the_run_continues(api, tmp_path):
    bad = artifact(tmp_path / "a", doi="10.1/boom")
    good = artifact(tmp_path / "b", doi="10.1/clean")

    assert run(api, bad, good, "--write") == 1

    assert not (bad.parent / "cited_retractions.json").exists()
    assert (good.parent / "cited_retractions.json").exists()


def test_folder_is_searched_for_artifacts(api, tmp_path):
    artifact(tmp_path / "run" / "x", doi="10.1/clean")
    artifact(tmp_path / "run" / "y", doi="10.1/flagged")

    assert run(api, tmp_path / "run", "--write") == 0

    assert sorted(api["calls"]) == ["10.1/clean", "10.1/flagged"]


def test_broken_artifact_is_an_error(api, tmp_path):
    path = tmp_path / "artifact.json"
    path.write_text("{}")

    assert run(api, path) == 1
