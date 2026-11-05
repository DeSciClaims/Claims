"""Frozen independent-replay reads reject changed rules and package links."""

import io
import hashlib
import json

import pytest

from competition.publication import canonical_sha256
from competition.drand import VerifiedBeacon
from competition.replay_inputs import (
    CompetitionReplayInputs, _gold_vote_message, validate_replay_snapshot,
    replay_paper_revisions, replay_rule_configurations, verify_replay_sample,
)
from competition.sample_release import PaperRecord, frozen_pool_hash, sample_receipt


def _snapshot():
    configuration = {"worker_policy": {"allowed_models": ["model-a"]}}
    snapshot = {
        "schema_version": "claims_competition_replay_snapshot_v1",
        "round_id": "week-1", "network": "testnet",
        "week_start": "2026-10-26", "deadline_at": "2026-10-26T12:00:00+00:00",
        "configuration_sha256": canonical_sha256(configuration),
        "deadline_block": 100, "reveal_block": 700,
        "reference_release_id": "bronze-release-1",
        "incumbent_hotkey": None,
        "baseline_package_sha256": "d" * 64,
        "reference_package_sha256": "d" * 64,
        "paper_pool_sha256": "a" * 64,
        "duplicate_review_links": [],
        "duplicate_review_sha256": canonical_sha256([]),
        "sample": [{"stage": "week_one", "paper_id": "paper-1",
                    "source_sha256": "b" * 64}],
        "stages": [{"stage": "week_one", "entries": [
            {"role": "challenger", "hotkey": "miner-1", "package_sha256": "c" * 64},
            {"role": "reference", "hotkey": "reference", "package_sha256": "d" * 64},
        ]}],
        "uploads": [{"hotkey": "miner-1", "package_sha256": "c" * 64,
                     "package_object_key": "sealed/miner-1", "package_size_bytes": 100,
                     "manifest": {"entrypoint": "miner:main"},
                     "salt_hex": "e" * 64, "commitment": "f" * 64,
                     "commit_block": 123, "status": "accepted"}],
    }
    rules = {
        "schema_version": "claims_competition_rules_v1",
        "configuration": configuration,
        **{key: snapshot[key] for key in (
            "round_id", "network", "week_start", "deadline_at",
            "deadline_block", "reveal_block",
            "configuration_sha256", "reference_release_id", "incumbent_hotkey",
            "baseline_package_sha256", "reference_package_sha256",
        )},
    }
    snapshot["rules_public"] = rules
    snapshot["rules_sha256"] = canonical_sha256(rules)
    return snapshot


def _amended_snapshot():
    snapshot = _snapshot()
    old_rules = dict(snapshot["rules_public"])
    old_configuration = old_rules["configuration"]
    configuration = {**old_configuration, "judge": "replacement"}
    rules = {**old_rules, "configuration": configuration,
             "configuration_sha256": canonical_sha256(configuration)}
    snapshot.update(rules_revision=2, rules_revision_sha256="a" * 64,
                    rules_public=rules, rules_sha256=canonical_sha256(rules),
                    configuration_sha256=canonical_sha256(configuration))
    snapshot["rules_revisions"] = [{
        "rules_revision": revision, "rules_revision_sha256": digest,
        "rules_sha256": canonical_sha256(document),
        "configuration_sha256": canonical_sha256(document["configuration"]),
        "configuration": document["configuration"],
    } for revision, digest, document in [(1, None, old_rules), (2, "a" * 64, rules)]]
    snapshot["stages"][0].update(produced_rules_revision=1,
                                 configuration_sha256=canonical_sha256(old_configuration))
    snapshot["paper_evaluations"] = [{
        "stage": "week_one", "paper_id": "paper-1", "produced_rules_revision": 1,
        "configuration_sha256": canonical_sha256(old_configuration), "input_sha256": "b" * 64,
        "carried_forward": {"to_revision": 2, "from_revision": 1,
                            "configuration_sha256": canonical_sha256(old_configuration),
                            "input_sha256": "b" * 64, "job_id_map": {"old": "new"}},
    }]
    return snapshot


def test_replay_client_uses_get_only_and_validates_frozen_inputs():
    requests = []

    def respond(request, *, timeout):
        requests.append((request, timeout))
        assert request.get_method() == "GET"
        assert request.get_header("Authorization") == "Bearer replay-only"
        return io.BytesIO(json.dumps(_snapshot()).encode())

    client = CompetitionReplayInputs(
        "https://backend.example", "replay-only", open_request=respond,
    )
    assert client.snapshot("week-1")["uploads"][0]["package_object_key"] == "sealed/miner-1"
    assert requests[0][0].full_url.endswith("/replay/rounds/week-1/snapshot")
    assert requests[0][1] == 30


@pytest.mark.parametrize("revision,digest", [(True, None), (0, None), ("2", "a" * 64),
                                             (2, None), (2, "invalid"), (1, "a" * 64)])
def test_replay_snapshot_rejects_invalid_rules_revision_evidence(revision, digest):
    snapshot = _snapshot()
    snapshot.update(rules_revision=revision, rules_revision_sha256=digest)
    with pytest.raises(ValueError, match="rules revision evidence"):
        validate_replay_snapshot(snapshot, "week-1")


def test_replay_snapshot_accepts_amended_generation_with_audit_digest():
    snapshot = _amended_snapshot()
    assert validate_replay_snapshot(snapshot, "week-1")["rules_revision"] == 2
    policies = replay_rule_configurations(snapshot)
    assert policies[1] != policies[2]
    assert replay_paper_revisions(snapshot) == {("week_one", "paper-1"): 1}
    snapshot["paper_evaluations"][0]["carried_forward"] = {}
    assert replay_paper_revisions(snapshot) == {("week_one", "paper-1"): 1}


@pytest.mark.parametrize("damage", ["missing_history", "missing_papers", "missing_historical_policy",
                                    "changed_policy", "changed_paper", "changed_stage", "changed_carry", "changed_input"])
def test_replay_snapshot_rejects_unavailable_or_changed_historical_scoring_provenance(damage):
    snapshot = _amended_snapshot()
    if damage == "missing_history":
        snapshot.pop("rules_revisions")
    elif damage == "missing_papers":
        snapshot.pop("paper_evaluations")
    elif damage == "missing_historical_policy":
        snapshot["rules_revisions"] = snapshot["rules_revisions"][1:]
    elif damage == "changed_policy":
        snapshot["rules_revisions"][0]["configuration"] = {"worker_policy": {"allowed_models": ["unapproved"]}}
    elif damage == "changed_paper":
        snapshot["paper_evaluations"][0]["configuration_sha256"] = snapshot["configuration_sha256"]
    elif damage == "changed_stage":
        snapshot["stages"][0]["produced_rules_revision"] = 9
    elif damage == "changed_carry":
        snapshot["paper_evaluations"][0]["carried_forward"]["from_revision"] = 2
    else:
        snapshot["paper_evaluations"][0]["carried_forward"]["input_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="replay"):
        validate_replay_snapshot(snapshot, "week-1")


def test_replay_client_checks_entire_released_pool_hash():
    papers = [PaperRecord("paper-1", "a" * 64), PaperRecord("paper-2", "b" * 64)]
    digest = frozen_pool_hash(papers)
    body = {
        "round_id": "week-1", "reference_release_id": "reference-v1",
        "paper_pool_sha256": digest,
        "papers": [{"paper_id": row.paper_id, "source_sha256": row.source_sha256}
                   for row in papers],
    }
    requests = []

    def respond(request, *, timeout):
        requests.append(request.full_url)
        assert request.get_method() == "GET" and timeout == 30
        return io.BytesIO(json.dumps(body).encode())

    client = CompetitionReplayInputs(
        "https://backend.example", "replay-only", open_request=respond,
    )
    assert client.paper_pool(
        "week-1", reference_release_id="reference-v1", expected_sha256=digest,
    ) == papers
    assert requests == [
        "https://backend.example/internal/competition/replay/rounds/week-1/paper-pool",
    ]
    body["papers"][0]["source_sha256"] = "c" * 64
    with pytest.raises(ValueError, match="frozen hash"):
        client.paper_pool(
            "week-1", reference_release_id="reference-v1", expected_sha256=digest,
        )


def test_replay_client_checks_qualification_source_rounds_against_deadline():
    body = {
        "round_id": "week-1", "window_start": "2026-10-23T12:00:00+00:00",
        "cutoff": "2026-10-30T12:00:00+00:00",
        "hotkeys": [{
            "hotkey": "miner-1", "qualified_round_count": 2, "qualified": True,
            "qualifying_rounds": [
                {"round_id": "vote-1", "issued_at": "2026-10-24T12:00:00+00:00",
                 "correct_count": 3, "total_count": 4},
                {"round_id": "vote-2", "issued_at": "2026-10-25T12:00:00+00:00",
                 "correct_count": 4, "total_count": 5},
            ],
        }],
    }
    requests = []

    def respond(request, *, timeout):
        requests.append(request.full_url)
        assert timeout == 30 and request.get_method() == "GET"
        return io.BytesIO(json.dumps(body).encode())

    client = CompetitionReplayInputs(
        "https://backend.example", "replay-only", open_request=respond,
    )
    assert client.qualification_evidence(
        "week-1", hotkeys=["miner-1"], deadline_at="2026-10-30T12:00:00+00:00",
    ) == body
    assert requests == [
        "https://backend.example/internal/competition/replay/rounds/"
        "week-1/qualification?hotkey=miner-1",
    ]
    body["hotkeys"][0]["qualifying_rounds"][1]["issued_at"] = "2026-10-30T12:00:00+00:00"
    with pytest.raises(ValueError, match="outside the frozen window"):
        client.qualification_evidence(
            "week-1", hotkeys=["miner-1"], deadline_at="2026-10-30T12:00:00+00:00",
        )
    body["hotkeys"][0]["qualifying_rounds"][1]["issued_at"] = "2026-10-25T12:00:00+00:00"
    body["hotkeys"][0]["qualifying_rounds"][1]["correct_count"] = 3
    with pytest.raises(ValueError, match="source round is malformed"):
        client.qualification_evidence(
            "week-1", hotkeys=["miner-1"], deadline_at="2026-10-30T12:00:00+00:00",
        )
    body["hotkeys"][0]["qualifying_rounds"][1]["correct_count"] = 4
    body["hotkeys"][0]["qualified"] = False
    with pytest.raises(ValueError, match="verdict differs"):
        client.qualification_evidence(
            "week-1", hotkeys=["miner-1"], deadline_at="2026-10-30T12:00:00+00:00",
        )


def test_replay_client_reads_certified_bank_without_primary_write_token():
    body = {"round_id": "week-1", "certified": [{"challenge_id": "case-1"}]}
    requests = []

    def respond(request, *, timeout):
        requests.append(request)
        assert timeout == 30 and request.get_method() == "GET"
        return io.BytesIO(json.dumps(body).encode())

    client = CompetitionReplayInputs(
        "https://backend.example", "replay-only", open_request=respond,
    )
    assert client.certified_gold_challenges("week-1") == body["certified"]
    assert requests[0].full_url.endswith(
        "/replay/rounds/week-1/certified-gold-challenges",
    )
    body["round_id"] = "another"
    with pytest.raises(ValueError, match="malformed"):
        client.certified_gold_challenges("week-1")


def test_replay_client_reads_frozen_gold_voters():
    body = {"round_id": "week-1", "voters": [
        {"voter_hotkey": "miner-1", "team_id": "team-1"},
    ]}

    def respond(request, *, timeout):
        assert timeout == 30 and request.get_method() == "GET"
        assert request.full_url.endswith("/replay/rounds/week-1/gold-voters")
        return io.BytesIO(json.dumps(body).encode())

    client = CompetitionReplayInputs(
        "https://backend.example", "replay-only", open_request=respond,
    )
    assert client.gold_voters("week-1") == body["voters"]
    body["voters"].append({"voter_hotkey": "miner-2", "team_id": ""})
    with pytest.raises(ValueError, match="malformed"):
        client.gold_voters("week-1")


def test_replay_client_verifies_closed_gold_votes_and_exclusions():
    message = _gold_vote_message("week-1", "case-1", "voter-1", "challenger")
    body = {
        "round_id": "week-1",
        "cases": [{
            "case_id": "case-1", "paper_id": "paper-1",
            "challenger_hotkey": "miner-1", "incumbent_hotkey": "reference",
            "excluded_team_ids": ["team-miner-1"],
            "case_public": {"statement": "Claim"}, "review_purposes": ["check"],
        }],
        "voters": [{"voter_hotkey": "voter-1", "team_id": "team-voter"}],
        "votes": [{
            "case_id": "case-1", "voter_hotkey": "voter-1",
            "voter_team_id": "team-voter", "choice": "challenger",
            "signed_payload_sha256": hashlib.sha256(message).hexdigest(),
            "signature_hex": "a" * 128,
        }],
    }
    requests = []

    def respond(request, *, timeout):
        requests.append(request.full_url)
        assert request.get_method() == "GET" and timeout == 30
        return io.BytesIO(json.dumps(body).encode())

    client = CompetitionReplayInputs(
        "https://backend.example", "replay-only", open_request=respond,
    )
    def verify(hotkey, payload, signature):
        return hotkey == "voter-1" and payload == message and signature == bytes.fromhex("a" * 128)

    assert client.closed_gold_review("week-1", verify_signature=verify) == body
    assert requests[0].endswith("/replay/rounds/week-1/gold-review")
    body["votes"][0]["choice"] = "incumbent"
    with pytest.raises(ValueError, match="signed choice"):
        client.closed_gold_review("week-1", verify_signature=verify)
    body["votes"][0]["choice"] = "challenger"
    body["cases"][0]["excluded_team_ids"].append("team-voter")
    with pytest.raises(ValueError, match="qualified case"):
        client.closed_gold_review("week-1", verify_signature=verify)


def test_replay_client_tallies_verified_gold_choices_with_zero_case_finalist():
    cases = [{
        "case_id": "case-1", "paper_id": "paper-1",
        "challenger_hotkey": "miner-1", "incumbent_hotkey": "reference",
        "excluded_team_ids": ["team-miner-1"],
        "case_public": {"statement": "Claim"}, "review_purposes": ["check"],
    }]
    voters = [{"voter_hotkey": f"voter-{i}", "team_id": f"team-voter-{i}"}
              for i in range(11)]
    votes = []
    for voter in voters:
        hotkey = voter["voter_hotkey"]
        message = _gold_vote_message("week-1", "case-1", hotkey, "challenger")
        votes.append({
            "case_id": "case-1", "voter_hotkey": hotkey,
            "voter_team_id": voter["team_id"], "choice": "challenger",
            "signed_payload_sha256": hashlib.sha256(message).hexdigest(),
            "signature_hex": "a" * 128,
        })
    body = {"round_id": "week-1", "cases": cases, "voters": voters, "votes": votes}

    def respond(request, *, timeout):
        if request.full_url.endswith("/snapshot"):
            return io.BytesIO(json.dumps(_snapshot()).encode())
        return io.BytesIO(json.dumps(body).encode())

    client = CompetitionReplayInputs(
        "https://backend.example", "replay-only", open_request=respond,
    )
    tally = client.closed_gold_tally(
        "week-1", finalist_hotkeys=["miner-1", "miner-2"],
        verify_signature=lambda *_: True,
    )
    assert tally["case_decisions"]["case-1"]["choice"] == "challenger"
    assert tally["gold_by_hotkey"]["miner-1"]["sampled_cases"] == 1
    assert tally["gold_by_hotkey"]["miner-2"]["sampled_cases"] == 0
    assert tally["gold_by_hotkey"]["miner-2"]["refusal"] is False


def test_replay_snapshot_rejects_changed_rule_and_missing_package():
    snapshot = _snapshot()
    snapshot["rules_public"]["configuration"]["worker_policy"]["allowed_models"] = ["changed"]
    with pytest.raises(ValueError, match="frozen round rules"):
        validate_replay_snapshot(snapshot, "week-1")
    snapshot = _snapshot()
    snapshot["uploads"] = []
    with pytest.raises(ValueError, match="lacks a selected sealed package"):
        validate_replay_snapshot(snapshot, "week-1")
    snapshot = _snapshot()
    snapshot["duplicate_review_links"].append({"left": "a", "right": "b", "reason": "copy"})
    with pytest.raises(ValueError, match="frozen round rules"):
        validate_replay_snapshot(snapshot, "week-1")
    snapshot = _snapshot()
    snapshot["sample"].append(dict(snapshot["sample"][0]))
    with pytest.raises(ValueError, match="repeats a paper"):
        validate_replay_snapshot(snapshot, "week-1")
    snapshot = _snapshot()
    snapshot["uploads"][0]["status"] = "duplicate"
    with pytest.raises(ValueError, match="excluded duplicate"):
        validate_replay_snapshot(snapshot, "week-1")


def test_replay_snapshot_binds_identity_to_hashed_production_rules():
    snapshot = _snapshot()
    validate_replay_snapshot(snapshot, "week-1")
    for field, changed in (
        ("reference_release_id", "other-release"),
        ("baseline_package_sha256", "0" * 64),
        ("deadline_block", 101),
    ):
        altered = {**snapshot, field: changed}
        with pytest.raises(ValueError, match="identity differs"):
            validate_replay_snapshot(altered, "week-1")


def test_replay_client_rejects_unprotected_backend_url():
    with pytest.raises(ValueError, match="private HTTPS"):
        CompetitionReplayInputs("http://backend.example", "replay-only")


def test_replay_sample_accepts_backend_stage_order_and_rejects_changed_position():
    signature = b"x" * 96
    beacon = VerifiedBeacon(
        42, hashlib.sha256(signature).digest(), signature.hex(),
        "a" * 64, "b" * 96, 123456,
    )
    pool = [PaperRecord(f"paper-{index}", format(index + 1, "064x"))
            for index in range(3)]
    draw = sample_receipt(
        papers=pool, beacon=beacon, prescreen_count=1,
        screen_count=1, final_count=1,
    )
    snapshot = {
        "deadline_block": 100, "reveal_block": 700,
        **{key: value for key, value in draw.items() if key != "papers"},
        "sample": sorted(draw["papers"], key=lambda row: (row["stage"], row["position"])),
        "stages": [],
    }
    verify_replay_sample(snapshot, paper_pool=pool, verified_beacon=beacon)
    snapshot["sample"][0]["position"] += 1
    with pytest.raises(ValueError, match="independently redrawn"):
        verify_replay_sample(snapshot, paper_pool=pool, verified_beacon=beacon)


def test_replay_waiver_must_match_backend_snapshot_and_testnet_network():
    policy = {"schema_version": "claims_competition_qualification_v1", "source": "backend",
              "network": "testnet", "required_consensus_rounds": 0, "window_days": 7}
    body = {"round_id": "week-1", "window_start": "2026-10-23T12:00:00+00:00",
            "cutoff": "2026-10-30T12:00:00+00:00", "qualification_policy": policy,
            "hotkeys": [{"hotkey": "miner-1", "qualified_round_count": 0,
                         "qualified": True, "qualifying_rounds": []}]}
    client = CompetitionReplayInputs("https://backend.example", "replay-only",
        open_request=lambda *a, **kw: io.BytesIO(json.dumps(body).encode()))
    args = dict(hotkeys=["miner-1"], deadline_at=body["cutoff"])
    assert client.qualification_evidence("week-1", **args, network="testnet", qualification_policy=policy) == body
    with pytest.raises(ValueError, match="differs from the backend snapshot"):
        client.qualification_evidence("week-1", **args)
    with pytest.raises(ValueError, match="invalid backend qualification policy"):
        client.qualification_evidence("week-1", **args, network="mainnet", qualification_policy=policy)
    strict = {**policy, "required_consensus_rounds": 2}
    body["qualification_policy"] = strict
    with pytest.raises(ValueError, match="verdict differs"):
        client.qualification_evidence("week-1", **args, network="testnet", qualification_policy=strict)
