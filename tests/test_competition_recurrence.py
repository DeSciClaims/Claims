import copy
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from competition.recurrence import RecurringRounds, next_window, render_round, validate_profile
from competition.round_setup import round_id_for_draft
from test_competition_production_handoff import _fixture, _plan


def profile():
    return dict(schema_version="claims_competition_recurrence_v1", enabled=True,
                network="testnet", profile_id="daily", cadence="daily", deadline_utc="12:00",
                intake_seconds=43200, minimum_intake_seconds=39600,
                round_config="daily.round-config.json", reveal_delay_blocks=1,
                duplicate_verdict_delay_seconds=300, duplicate_dispute_seconds=3300,
                publication_delay_seconds=7200, activation_delay_seconds=60,
                block_time_sample_blocks=60, retry_seconds=60)


def setup(tmp_path, *, state=None, reader=None, publication=None):
    p = profile()
    path = tmp_path / "recurrence.json"
    path.write_text(json.dumps(p))
    template = dict(network="testnet", baseline_selection="active_production",
                    policy_public={"winner_selection":"silver_tiebreak"},
                    configuration_spec={"models":["pinned/model"]})
    (tmp_path / p["round_config"]).write_text(json.dumps(template))
    state = state or dict(network="testnet", active_round=None, latest_round=None, latest_published=None)
    calls = []
    backend = SimpleNamespace(recurrence_state=lambda network: state)
    def prepare(draft):
        calls.append(copy.deepcopy(draft))
        state["active_round"] = dict(round_id=draft["round_id"])
        return dict(round_id=draft["round_id"])
    kwargs = dict(profile_path=path, network="testnet", backend=backend, rounds_dir=tmp_path / "rounds",
                  reader=reader or SimpleNamespace(read_active=lambda: None), expected_signer="signer",
                  publication_for_round=publication or (lambda _: None),
                  estimate_deadline_block=lambda *_: 1000, prepare=prepare)
    return RecurringRounds(**kwargs), kwargs, state, calls


def test_calendar_skips_missed_intake_and_supports_weekly():
    p = profile(); validate_profile(p, "testnet")
    now = datetime(2026, 11, 3, 0, tzinfo=UTC)
    assert next_window(p, now) == (now, now+timedelta(hours=12))
    assert next_window(p, now+timedelta(hours=2))[0] == now+timedelta(days=1)
    weekly = {**p,"network":"mainnet","cadence":"weekly","weekday":0,
              "intake_seconds":604800,"minimum_intake_seconds":7200}
    validate_profile(weekly,"mainnet")
    assert next_window(weekly, now)[1] == datetime(2026,11,9,12,tzinfo=UTC)
    with pytest.raises(ValueError): validate_profile({**p,"network":"mainnet"},"mainnet")
    with pytest.raises(ValueError): validate_profile({**p,"round_config":"../unsafe.json"},"testnet")


def test_open_round_is_frozen_once_and_restart_does_not_duplicate(tmp_path):
    scheduler, kwargs, state, calls = setup(tmp_path)
    now = datetime(2026,11,3,0,tzinfo=UTC)
    result = scheduler.tick(now=now)
    assert result["status"] == "recurrence_opened"
    draft = calls[0]
    assert draft["round_id"] == "round_20261103T120000Z_testnet"
    assert draft["deadline_block"] == 1000 and draft["reveal_block"] == 1001
    assert draft["configuration_spec"] == {"models":["pinned/model"]}
    assert draft["policy_public"]["schedule"]["production_activate_at"] == "2026-11-03T14:01:00+00:00"
    assert scheduler.tick(now=now+timedelta(seconds=10)) is None
    assert RecurringRounds(**kwargs).tick(now=now)["status"] == "recurrence_waiting_for_round"
    assert len(calls) == 1


def test_setup_crash_reuses_saved_candidate_and_chain_estimate(tmp_path):
    scheduler, kwargs, state, calls = setup(tmp_path)
    def crash(draft):
        calls.append(copy.deepcopy(draft))
        raise ConnectionError("backend unavailable after freezing")
    scheduler.prepare = crash
    now=datetime(2026,11,3,0,tzinfo=UTC)
    with pytest.raises(ConnectionError): scheduler.tick(now=now)
    template=tmp_path/"daily.round-config.json"
    changed=json.loads(template.read_text());changed["configuration_spec"]["models"]=["different/model"]
    template.write_text(json.dumps(changed))
    restarted=RecurringRounds(**{**kwargs,"estimate_deadline_block":lambda *_: pytest.fail("re-estimated frozen block")})
    assert restarted.tick(now=now+timedelta(minutes=1))["status"] == "recurrence_opened"
    assert calls[0] == calls[1]
    assert (tmp_path/"rounds/.recurrence/daily"/f'{calls[0]["round_id"]}.candidate.json').stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("decision,status",[("failure","recurrence_paused_after_failure"),("no_contest","recurrence_opened")])
def test_failure_pauses_but_empty_intake_allows_next_day(tmp_path,decision,status):
    state=dict(network="testnet",active_round=None,latest_published=None,
               latest_round=dict(round_id="previous",status="failed",decision=decision,deadline_at="2026-11-02T12:00:00Z"))
    scheduler,kwargs,_,calls=setup(tmp_path,state=state)
    assert scheduler.tick(now=datetime(2026,11,3,0,tzinfo=UTC))["status"] == status
    if decision == "failure":
        p=profile();p["acknowledged_failed_round_id"]="previous"
        kwargs["profile_path"].write_text(json.dumps(p))
        assert RecurringRounds(**kwargs).tick(now=datetime(2026,11,3,0,tzinfo=UTC))["status"] == "recurrence_opened"


def test_retained_incumbent_must_have_latest_signed_handoff(tmp_path):
    signer,record,stage,receipt=_fixture("incumbent_continues",accelerated=True)
    now=datetime(2026,11,3,0,tzinfo=UTC)
    plan=_plan(signer,record,stage,receipt,now)
    active=SimpleNamespace(**{k:getattr(plan,k) for k in ("round_id","receipt_sha256","hotkey","package_sha256")},
                           latest_activation_at=plan.activate_at)
    state=dict(network="testnet",active_round=None,latest_round=record,latest_published=record)
    scheduler,_,_,calls=setup(tmp_path,state=state,reader=SimpleNamespace(read_active=lambda:active),publication=lambda _:receipt)
    scheduler.expected_signer=signer.ss58_address
    scheduler.backend.get_round=lambda _:record;scheduler.backend.list_stages=lambda _:[stage]
    active.round_id="older-round"
    assert scheduler.tick(now=now)["status"] == "recurrence_waiting_for_handoff"
    assert not calls
    scheduler.backend.get_round=lambda _:pytest.fail("refetched verified immutable receipt")
    active.round_id=plan.round_id
    assert scheduler.tick(now=now+timedelta(minutes=1))["status"] == "recurrence_opened"


def test_idle_or_disabled_never_queries_chain_or_backend(tmp_path):
    scheduler,kwargs,_,calls=setup(tmp_path)
    scheduler.backend.recurrence_state=lambda _:pytest.fail("unnecessary query")
    assert scheduler.tick(now=datetime(2026,11,3,13,tzinfo=UTC))["status"] == "recurrence_waiting"
    p=profile();p["enabled"]=False;kwargs["profile_path"].write_text(json.dumps(p))
    assert scheduler.tick(now=datetime(2026,11,4,0,tzinfo=UTC))["status"] == "recurrence_disabled"


def test_candidate_cannot_be_symlinked(tmp_path):
    scheduler,_,_,_=setup(tmp_path)
    target=tmp_path/"other.json";target.write_text('{}')
    root=tmp_path/"rounds/.recurrence/daily";root.mkdir(parents=True,mode=0o700)
    (root/"round_20261103T120000Z_testnet.candidate.json").symlink_to(target)
    with pytest.raises(OSError): scheduler.tick(now=datetime(2026,11,3,0,tzinfo=UTC))
