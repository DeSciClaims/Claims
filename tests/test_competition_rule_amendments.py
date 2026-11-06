import copy
import json
import os
from pathlib import Path

import pytest

from competition import rule_amendments as amendments
from competition.intake_lock import CompetitionBackend
from competition.publication import FROZEN_FIELDS, canonical_sha256, publication_bundle
from competition.rule_amendments import (
    RulesAmendmentClient, active_round_file, configuration_changes,
    read_private_round, require_round_directory_owner, sync_current_rules,
)
from neurons import competition_round_amend as cli


def write(path, data, mode=0o600):
    path.write_text(json.dumps(data))
    path.chmod(mode)


class Backend:
    def __init__(self, row):
        self.row, self.calls = row, 0

    def get_round(self, rid):
        self.calls += 1
        assert rid == self.row["round_id"]
        return copy.deepcopy(self.row)


@pytest.fixture
def revision(tmp_path):
    original = {"worker_policy": {"allowed_models": ["model"], "dollar_limit_usd": "0.1"},
                "judges": ["old-model"]}
    changed = {**original, "judges": ["new-model"]}
    document = {"round_id": "round-1", "configuration": original,
                "configuration_sha256": canonical_sha256(original),
                "baseline_upload": {"package_sha256": "a" * 64,
                                    "package_object_key": "sealed/private/reference.whl"}}
    rules = {"round_id": "round-1", "network": "testnet", "configuration": changed,
             "configuration_sha256": canonical_sha256(changed), "policy": {"unchanged": True}}
    row = {"round_id": "round-1", "network": "testnet", "rules_revision": 2,
           "rules_revision_sha256": "d" * 64, "rules_public": rules,
           "rules_sha256": canonical_sha256(rules), "configuration_sha256": canonical_sha256(changed),
           "evaluation_binding": None}
    write(tmp_path / "round-1.json", document)
    return Backend(row), document


def test_reconcile_preserves_original_and_trusted_receipts(tmp_path, revision):
    backend, original = revision
    source = (tmp_path / "round-1.json").read_bytes()
    row = sync_current_rules(backend, backend.row, directory=tmp_path)
    assert row["rules_revision"] == 2
    assert (tmp_path / "round-1.json").read_bytes() == source
    assert active_round_file(tmp_path, "round-1").name == "round-1.r2.json"
    private = read_private_round(tmp_path, "round-1")
    assert private["configuration"] == row["rules_public"]["configuration"]
    assert private["baseline_upload"] == original["baseline_upload"]
    assert "sealed/private/reference.whl" not in (tmp_path / "round-1.rules-r2.json").read_text()
    assert (tmp_path / "round-1.r2.json").stat().st_mode & 0o777 == 0o600
    before = (tmp_path / "round-1.r2.json").stat().st_mtime_ns
    sync_current_rules(backend, row, directory=tmp_path)
    assert (tmp_path / "round-1.r2.json").stat().st_mtime_ns == before


def test_reconcile_keeps_an_identical_valid_pointer_inode_owner_and_mtime(tmp_path, revision):
    backend, _ = revision
    sync_current_rules(backend, backend.row, directory=tmp_path)
    pointer = tmp_path / "round-1.active.json"
    before = pointer.stat()
    original = pointer.read_bytes()
    sync_current_rules(backend, backend.row, directory=tmp_path)
    after = pointer.stat()
    assert (after.st_ino, after.st_uid, after.st_gid, after.st_mtime_ns, after.st_mode) == (
        before.st_ino, before.st_uid, before.st_gid, before.st_mtime_ns, before.st_mode)
    assert pointer.read_bytes() == original


@pytest.mark.parametrize("effective_uid", [0, 1234])
def test_wrong_owner_is_rejected_before_backend_read_lock_or_temporary_writes(tmp_path, revision, monkeypatch, effective_uid):
    from types import SimpleNamespace
    backend, _ = revision
    original = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    real_stat = Path.stat
    def stat(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        return SimpleNamespace(st_mode=info.st_mode, st_uid=4321) if path == tmp_path else info
    with monkeypatch.context() as patch:
        patch.setattr(Path, "stat", stat)
        patch.setattr(amendments.os, "geteuid", lambda: effective_uid)
        patch.setattr(amendments, "_revision_lock", lambda *_: pytest.fail("writer lock reached before owner check"))
        patch.setattr(amendments.tempfile, "mkstemp", lambda *a, **k: pytest.fail("temporary file reached before owner check"))
        with pytest.raises(ValueError, match="owned by the current service user"):
            sync_current_rules(backend, backend.row, directory=tmp_path)
    assert backend.calls == 0
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == original


def test_directory_owner_preflight_rejects_a_symlink(tmp_path):
    target = tmp_path / "rounds"
    target.mkdir()
    link = tmp_path / "rounds-link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="owned by the current service user"):
        require_round_directory_owner(link)


def test_identical_pointer_is_validated_before_noop(tmp_path, revision, monkeypatch):
    backend, _ = revision
    sync_current_rules(backend, backend.row, directory=tmp_path)
    pointer = tmp_path / "round-1.active.json"
    document = json.loads(pointer.read_text())
    original_inode = pointer.stat().st_ino
    private = tmp_path / "round-1.r2.json"
    private.unlink()
    monkeypatch.setattr(amendments.tempfile, "mkstemp", lambda *a, **k: pytest.fail("invalid pointer must not be rewritten"))
    with pytest.raises(FileNotFoundError):
        amendments._activate(pointer, document)
    assert pointer.stat().st_ino == original_inode


def test_reconcile_resumes_after_crash_before_pointer_activation(tmp_path, revision, monkeypatch):
    backend, _ = revision
    activate = amendments._activate
    monkeypatch.setattr(amendments, "_activate", lambda *_: (_ for _ in ()).throw(OSError("interrupted")))
    with pytest.raises(OSError, match="interrupted"):
        sync_current_rules(backend, backend.row, directory=tmp_path)
    assert active_round_file(tmp_path, "round-1").name == "round-1.json"
    assert (tmp_path / "round-1.r2.json").exists()
    monkeypatch.setattr(amendments, "_activate", activate)
    sync_current_rules(backend, backend.row, directory=tmp_path)
    assert active_round_file(tmp_path, "round-1").name == "round-1.r2.json"


@pytest.mark.parametrize("tamper", ["configuration", "rules_hash", "digest", "worker_policy"])
def test_reconcile_rejects_invalid_or_incompatible_revision(tmp_path, revision, tamper):
    backend, _ = revision
    if tamper == "configuration":
        backend.row["rules_public"]["configuration"]["judges"] = ["unapproved"]
    elif tamper == "rules_hash":
        backend.row["rules_sha256"] = "f" * 64
    elif tamper == "digest":
        backend.row["rules_revision_sha256"] = None
    else:
        backend.row["rules_public"]["configuration"]["worker_policy"] = {"dollar_limit_usd": "100"}
        backend.row["configuration_sha256"] = canonical_sha256(backend.row["rules_public"]["configuration"])
        backend.row["rules_public"]["configuration_sha256"] = backend.row["configuration_sha256"]
        backend.row["rules_sha256"] = canonical_sha256(backend.row["rules_public"])
    with pytest.raises(ValueError):
        sync_current_rules(backend, backend.row, directory=tmp_path)
    assert not (tmp_path / "round-1.active.json").exists()


def test_reconcile_rebases_bound_incumbent_without_changing_receipt(tmp_path, revision):
    backend, original = revision
    original["baseline_pending"] = True
    write(tmp_path / "round-1.json", original)
    old_binding = {"round_id": "round-1", "network": "testnet", "incumbent_hotkey": "incumbent",
                   "configuration_sha256": original["configuration_sha256"],
                   "baseline_package_sha256": "b" * 64, "reference_package_sha256": "a" * 64}
    receipt = {"binding": old_binding,
               "baseline_upload": {"hotkey": "incumbent", "package_sha256": "b" * 64,
                                   "package_object_key": "sealed/incumbent.whl"},
               "reference_upload": original["baseline_upload"]}
    write(tmp_path / "round-1.evaluation.json", receipt)
    backend.row["evaluation_binding"] = {**old_binding,
        "configuration_sha256": backend.row["configuration_sha256"], "rules_sha256": backend.row["rules_sha256"]}
    sync_current_rules(backend, backend.row, directory=tmp_path)
    private = read_private_round(tmp_path, "round-1")
    assert private["baseline_upload"] == receipt["baseline_upload"]
    assert private["reference_upload"] == receipt["reference_upload"]
    assert private["binding"] == backend.row["evaluation_binding"]
    assert json.loads((tmp_path / "round-1.evaluation.json").read_text()) == receipt


def test_pending_baseline_can_bind_after_revision_then_be_amended_again(tmp_path, revision):
    backend, original = revision
    original["baseline_pending"] = True
    write(tmp_path / "round-1.json", original)
    sync_current_rules(backend, backend.row, directory=tmp_path)
    bound = {"round_id": "round-1", "incumbent_hotkey": "incumbent",
             "baseline_package_sha256": "b" * 64, "reference_package_sha256": "a" * 64,
             "configuration_sha256": backend.row["configuration_sha256"]}
    receipt = {"binding": bound,
               "baseline_upload": {"hotkey": "incumbent", "package_sha256": "b" * 64},
               "reference_upload": original["baseline_upload"]}
    write(tmp_path / "round-1.evaluation-r2.json", receipt)
    backend.row["evaluation_binding"] = bound
    sync_current_rules(backend, backend.row, directory=tmp_path)
    assert read_private_round(tmp_path, "round-1")["binding"] == bound
    backend.row.update(rules_revision=3, rules_revision_sha256="e" * 64)
    backend.row["rules_public"]["configuration"]["judges"] = ["another-model"]
    backend.row["configuration_sha256"] = canonical_sha256(backend.row["rules_public"]["configuration"])
    backend.row["rules_public"]["configuration_sha256"] = backend.row["configuration_sha256"]
    backend.row["rules_sha256"] = canonical_sha256(backend.row["rules_public"])
    backend.row["evaluation_binding"] = {**bound, "configuration_sha256": backend.row["configuration_sha256"]}
    sync_current_rules(backend, backend.row, directory=tmp_path)
    assert read_private_round(tmp_path, "round-1")["binding"] == backend.row["evaluation_binding"]
    assert not (tmp_path / "round-1.evaluation.json").exists()


def test_prepare_evaluation_binds_an_amended_open_round_and_retries_safely(tmp_path, revision, monkeypatch):
    from types import SimpleNamespace
    from competition.evaluation_binding import prepare_evaluation

    backend, original = revision
    original["baseline_pending"] = True
    original["baseline_upload"].update(hotkey="reference", team_id="claims")
    write(tmp_path / "round-1.json", original)
    backend.row.update(deadline_at="2026-11-05T12:00:00+00:00",
                       reference_package_sha256="a" * 64)
    backend.row["rules_public"]["policy"]["baseline_selection"] = "previous_completed_round"
    backend.row["rules_sha256"] = canonical_sha256(backend.row["rules_public"])
    backend.evaluation_gate = lambda _: {"ready": True, "predecessor": None}
    bindings = []
    def bind(_, binding):
        bindings.append(binding)
        backend.row["evaluation_binding"] = binding
        return binding
    backend.bind_evaluation = bind
    monkeypatch.setenv("CLAIMS_COMPETITION_ROUNDS_DIR", str(tmp_path))
    monkeypatch.setenv("CLAIMS_COMPETITION_PRODUCTION_ROOT", str(tmp_path / "production"))
    monkeypatch.setenv("CLAIMS_COMPETITION_PUBLISHER_HOTKEY", "signer")
    monkeypatch.setattr("competition.evaluation_binding.ProductionReleaseReader",
                        lambda _: SimpleNamespace(read_active=lambda: None))
    original_bytes = (tmp_path / "round-1.json").read_bytes()

    assert prepare_evaluation(backend.row, backend) == {"status": "evaluation_ready"}
    receipt = tmp_path / "round-1.evaluation-r2.json"
    assert receipt.exists() and not (tmp_path / "round-1.evaluation.json").exists()
    assert read_private_round(tmp_path, "round-1")["binding"] == bindings[0]
    assert prepare_evaluation(backend.row, backend) == {"status": "evaluation_ready"}
    assert len(bindings) == 1
    assert (tmp_path / "round-1.json").read_bytes() == original_bytes


def test_reconcile_refuses_a_backend_rollback(tmp_path, revision):
    backend, _ = revision
    sync_current_rules(backend, backend.row, directory=tmp_path)
    pointer = json.loads((tmp_path / "round-1.active.json").read_text())
    pointer["rules_revision"] = 3
    write(tmp_path / "round-1.active.json", pointer)
    with pytest.raises(ValueError, match="roll back"):
        sync_current_rules(backend, backend.row, directory=tmp_path)


def test_active_pointer_cannot_escape_round_directory(tmp_path, revision):
    backend, _ = revision
    sync_current_rules(backend, backend.row, directory=tmp_path)
    pointer = json.loads((tmp_path / "round-1.active.json").read_text())
    pointer["private_filename"] = "../outside.json"
    write(tmp_path / "round-1.active.json", pointer)
    with pytest.raises(ValueError, match="malformed"):
        active_round_file(tmp_path, "round-1")


def test_unamended_round_needs_no_backend_or_revision_files(tmp_path):
    class NeverCall:
        def get_round(self, _):
            pytest.fail("legacy round must not incur another backend read")
    row = {"round_id": "round-1", "network": "testnet"}
    assert sync_current_rules(NeverCall(), row, directory=tmp_path) == row
    assert not list(tmp_path.iterdir())


def test_diff_distinguishes_missing_fields_from_null():
    diff = configuration_changes({"removed": None, "nested": {"a/b": "before"}},
                                 {"added": None, "nested": {"a/b": "after"}})
    assert {r["path"] for r in diff} == {"/configuration/added", "/configuration/removed", "/configuration/nested/a~1b"}
    assert next(r for r in diff if r["path"].endswith("removed"))["after_exists"] is False


def test_admin_client_never_uses_controller_credential_for_mutation(monkeypatch):
    client = RulesAmendmentClient(base_url="https://backend.example")
    monkeypatch.setenv("CLAIMS_COMPETITION_CONTROLLER_TOKEN", "controller-secret")
    with pytest.raises(ValueError, match="administrator"):
        client.amend("round-1", expected_rules_sha256="a" * 64,
                     configuration={}, reason="change model", idempotency_key="op-1")


def test_admin_client_sends_default_resume_and_explicit_stage_restart():
    client = RulesAmendmentClient(base_url="https://backend.example", admin_token="admin-secret")
    payloads = []
    client._request = lambda path, *, payload: payloads.append(payload) or payload
    args = {"expected_rules_sha256": "a" * 64, "configuration": {},
            "reason": "Replace unavailable judge", "idempotency_key": "op-1"}
    client.amend("round-1", **args)
    client.amend("round-1", **args, resume_mode="restart_stage", restart_stage="screen")
    assert payloads[0]["resume_mode"] == "resume" and "restart_stage" not in payloads[0]
    assert payloads[1]["resume_mode"] == "restart_stage" and payloads[1]["restart_stage"] == "screen"
    for mode, stage in [("resume", "final"), ("restart_stage", None), ("restart_stage", "comparison")]:
        with pytest.raises(ValueError, match="resume or a valid"):
            client.amend("round-1", **args, resume_mode=mode, restart_stage=stage)


def test_controller_client_sends_explicit_configuration_generation():
    backend = CompetitionBackend(base_url="https://backend.example", controller_token="controller-secret")
    captured = []
    backend._request = lambda method, path, payload=None: captured.append((method, path, payload)) or {}
    assert backend.supports_rules_revisions
    backend.claim_job("round-1", worker_id="worker", lease_seconds=3600,
                      expected_configuration_sha256="c" * 64, expected_rules_revision=2)
    backend.freeze_paper_evaluation("round-1", "prescreen", "paper", input_sha256="d" * 64,
                                    result_private={}, expected_configuration_sha256="c" * 64,
                                    expected_rules_revision=2)
    fence = {"expected_configuration_sha256": "c" * 64, "expected_rules_revision": 2}
    backend.start_stage("round-1", "prescreen", [], **fence)
    backend.enqueue_jobs("round-1", [], **fence)
    backend.start_reference_rerun("round-1", {"hotkey": "ref", "package_sha256": "e" * 64,
                                            "team_id": "reference"}, **fence)
    assert all(payload["expected_configuration_sha256"] == "c" * 64 for _, _, payload in captured)
    assert all(payload["expected_rules_revision"] == 2 for _, _, payload in captured)
    backend.list_rule_amendments("round-1", limit=100, before_revision=3)
    assert captured[-1][1].endswith("/rules/amendments?limit=100&before_revision=3")


def test_cli_previews_then_applies_same_round_with_admin_client(tmp_path, monkeypatch, capsys, revision):
    backend, _ = revision
    old_rules = copy.deepcopy(backend.row["rules_public"])
    old_rules["configuration"]["judges"] = ["old-model"]
    old_rules["configuration_sha256"] = canonical_sha256(old_rules["configuration"])
    changed = backend.row["rules_public"]["configuration"]
    path = tmp_path / "config.json"
    write(path, {"configuration": changed})
    calls = []
    class Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
        def rules(self, rid):
            assert rid == "round-1"
            return old_rules
        def amend(self, rid, **kwargs):
            calls.append((rid, kwargs, self.kwargs))
            return {"round_id": rid, "rules_revision": 2, "changes": []}
    monkeypatch.setattr(cli, "RulesAmendmentClient", Client)
    args = ["--round-id", "round-1", "--backend-url", "https://backend.example",
            "--config", str(path), "--reason", "Replace unavailable judge"]
    assert cli.main(args) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["status"] == "preview" and not calls
    assert preview["resume_mode"] == "resume" and preview["restart_stage"] is None
    monkeypatch.setenv("CLAIMS_ADMIN_TOKEN", "administrator-secret")
    assert cli.main([*args, "--apply"]) == 0
    output = capsys.readouterr().out
    assert calls[0][0] == "round-1"
    assert calls[0][1]["expected_rules_sha256"] == canonical_sha256(old_rules)
    assert calls[0][1]["configuration"] == changed
    assert calls[0][1]["resume_mode"] == "resume"
    assert calls[0][2]["admin_token"] == "administrator-secret"
    assert "administrator-secret" not in output


def test_cli_stage_restart_is_explicit_even_with_unchanged_configuration(tmp_path, revision, monkeypatch, capsys):
    backend, _ = revision
    path = tmp_path / "config.json"
    write(path, {"configuration": backend.row["rules_public"]["configuration"]})
    requests = []
    class Client:
        def __init__(self, **_):
            pass
        def rules(self, _):
            return backend.row["rules_public"]
        def amend(self, rid, **request):
            requests.append(request)
            return {"round_id": rid, "rules_revision": 3, "resume_mode": request["resume_mode"],
                    "restart_stage": request["restart_stage"], "restarted_from_stage": "screen"}
    monkeypatch.setattr(cli, "RulesAmendmentClient", Client)
    monkeypatch.setenv("CLAIMS_ADMIN_TOKEN", "admin-secret")
    args = ["--round-id", "round-1", "--backend-url", "https://backend.example",
            "--config", str(path), "--reason", "Restart scoring after outage", "--restart-stage", "screen"]
    assert cli.main(args) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["resume_mode"] == "restart_stage" and preview["restart_stage"] == "screen"
    assert not requests
    assert cli.main([*args, "--apply"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["resume_mode"] == "restart_stage" and result["restarted_from_stage"] == "screen"
    assert requests[0]["restart_stage"] == "screen"
    with pytest.raises(SystemExit):
        cli.main([*args, "--resume"])


def test_amended_publication_signs_revision_and_requires_digest():
    record = {key: "present" for key in FROZEN_FIELDS}
    record.update(rules_revision=2, rules_revision_sha256="d" * 64)
    bundle = publication_bundle(record, results={"decision": "crowned"}, weights={"vector": []})
    assert bundle["rules_revision"] == 2 and bundle["rules_revision_sha256"] == "d" * 64
    record.pop("rules_revision_sha256")
    with pytest.raises(ValueError, match="revision digest"):
        publication_bundle(record, results={"decision": "crowned"}, weights={"vector": []})


def test_cli_model_rollback_does_not_replay_an_earlier_amendment(tmp_path, revision, monkeypatch, capsys):
    backend, _ = revision
    original = copy.deepcopy(backend.row["rules_public"])
    alternate = copy.deepcopy(original["configuration"])
    alternate["judges"] = ["alternate-model"]
    state = {"rules": original, "revision": 1, "receipts": {}}

    class Client:
        def __init__(self, **_):
            pass

        def rules(self, _):
            return state["rules"]

        def amend(self, rid, **request):
            key = request["idempotency_key"]
            if key not in state["receipts"]:
                assert request["expected_rules_sha256"] == canonical_sha256(state["rules"])
                configuration = request["configuration"]
                state["revision"] += 1
                state["rules"] = {**state["rules"], "configuration": configuration,
                                  "configuration_sha256": canonical_sha256(configuration)}
                state["receipts"][key] = {"round_id": rid, "rules_revision": state["revision"]}
            return state["receipts"][key]

    monkeypatch.setattr(cli, "RulesAmendmentClient", Client)
    monkeypatch.setenv("CLAIMS_ADMIN_TOKEN", "administrator-secret")
    path = tmp_path / "config.json"
    args = ["--round-id", "round-1", "--backend-url", "https://backend.example",
            "--config", str(path), "--reason", "Update judge availability", "--apply"]
    for configuration in (alternate, original["configuration"], alternate):
        write(path, {"configuration": configuration})
        assert cli.main(args) == 0
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [row["rules_revision"] for row in output] == [2, 3, 4]
    assert len(state["receipts"]) == 3
