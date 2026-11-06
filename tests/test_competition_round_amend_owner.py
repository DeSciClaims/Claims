"""Local installation must not mutate backend rules under the wrong account."""

import json
from types import SimpleNamespace

import pytest

from neurons import competition_round_amend as cli


@pytest.mark.parametrize("changed", [False, True])
def test_wrong_install_account_is_rejected_before_backend_access(tmp_path, monkeypatch, capsys, changed):
    config = tmp_path / "configuration.json"
    config.write_text(json.dumps({"worker_policy": {}, "judges": ["new" if changed else "old"]}))
    rounds = tmp_path / "rounds"
    rounds.mkdir()
    monkeypatch.setenv("CLAIMS_COMPETITION_CONTROLLER_TOKEN", "controller-token")

    def wrong_owner(directory):
        assert directory == rounds
        raise ValueError("round reconciliation must run as the directory owner")

    def no_backend(**_):
        pytest.fail("wrong local account must not contact the admin API")

    monkeypatch.setattr(cli, "require_round_directory_owner", wrong_owner)
    monkeypatch.setattr(cli, "RulesAmendmentClient", no_backend)
    assert cli.main([
        "--round-id", "round-1", "--backend-url", "https://backend.example",
        "--config", str(config), "--rounds-dir", str(rounds),
        "--reason", "Upgrade controller safeguards", "--apply",
    ]) == 1
    assert list(rounds.iterdir()) == []
    result = json.loads(capsys.readouterr().err)
    assert result["status"] == "amendment_error"
    assert "directory owner" in result["error"]


def test_owned_install_directory_can_apply_and_reconcile(tmp_path, monkeypatch, capsys):
    rounds = tmp_path / "rounds"
    rounds.mkdir()
    before = {"worker_policy": {}, "judges": ["old"]}
    after = {"worker_policy": {}, "judges": ["new"]}
    config = tmp_path / "configuration.json"
    config.write_text(json.dumps(after))
    calls = []
    row = {"round_id": "round-1"}

    class Client:
        def __init__(self, **_):
            pass

        def rules(self, rid):
            return {"round_id": rid, "configuration": before}

        def amend(self, rid, **kwargs):
            assert kwargs["configuration"] == after
            calls.append("amend")
            return {"round_id": rid, "rules_revision": 2}

    def sync(backend, record, *, directory):
        assert directory == rounds and record == row
        calls.append("sync")

    monkeypatch.setenv("CLAIMS_COMPETITION_CONTROLLER_TOKEN", "controller-token")
    monkeypatch.setattr(cli, "RulesAmendmentClient", Client)
    monkeypatch.setattr(cli, "sync_current_rules", sync)
    monkeypatch.setattr("competition.intake_lock.CompetitionBackend", lambda **_: SimpleNamespace(get_round=lambda _: row))
    assert cli.main([
        "--round-id", "round-1", "--backend-url", "https://backend.example",
        "--config", str(config), "--rounds-dir", str(rounds),
        "--reason", "Upgrade controller safeguards", "--apply",
    ]) == 0
    assert calls == ["amend", "sync"]
    assert json.loads(capsys.readouterr().out)["rules_revision"] == 2
