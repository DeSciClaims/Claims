"""Experimental replay preserves HOLD and permits only identical evidence reuse."""
from fractions import Fraction
import importlib
import pytest
from tests.source_tasks.test_source_scoring import generated  # noqa: F401
from tests.source_tasks.test_source_scoring import test_full_p005_pool_freezes_exact_scores_and_replays_without_provider as run_example
from validator.scientific_result_v1.source_tasks.contracts import ErrorRecord
from validator.scientific_result_v1.source_tasks.errors import BUDGETED_HOLD_CODE, validate_error

replay_module = importlib.import_module('validator.scientific_result_v1.source_tasks.replay')


def marker(**changes):
    return ErrorRecord(category='EVALUATOR', code=BUDGETED_HOLD_CODE, stage='VERIFICATION',
        message='Experimental profile remains a strict HOLD', retryable=False, output_refs=[]).model_copy(update=changes)


@pytest.fixture
def frozen(generated, tmp_path, monkeypatch):
    captured = []
    original = replay_module.replay
    def capture(store, ref):
        receipt = original(store, ref)
        captured.append((store, receipt))
        return receipt
    with monkeypatch.context() as patch:
        patch.setattr(replay_module, 'replay', capture)
        run_example(generated, tmp_path, 'perfect', Fraction(1), monkeypatch=monkeypatch)
    return captured[0]


def held(receipt):
    vectors = [vector.model_copy(update=dict(state='HOLD', content_prose=None, content_table=None,
        content=None, archive=None, output_audit=None, score=None, skipped_reason=None)) for vector in receipt.paper_scores]
    return receipt.model_copy(update=dict(state='HOLD', paper_scores=vectors, hold_reasons=[marker()]))


def test_marker_is_registered_only_for_its_evaluator_verification_stage():
    validate_error(marker())
    for changes in [dict(category='PIPELINE'), dict(stage='MECHANICAL'), dict(code='UNKNOWN_CODE')]:
        with pytest.raises(ValueError, match='Unregistered'):
            validate_error(marker(**changes))


def test_repeated_experimental_packet_refs_replay_without_rewriting_receipt(frozen):
    store, receipt = frozen
    candidate = held(receipt)
    candidate = candidate.model_copy(update={'assertion_packets': [*candidate.assertion_packets, candidate.assertion_packets[0]]})
    ref = store.put(candidate)
    before = store.read(ref)
    replayed = replay_module.replay(store, ref)
    assert replayed == candidate and store.read(ref) == before
    assert replayed.state == 'HOLD' and all(score.score is None for score in replayed.paper_scores)
    assert len(replayed.assertion_packets) == len(receipt.assertion_packets) + 1
    assert [v.task_bits for v in replayed.paper_scores] == [v.task_bits for v in receipt.paper_scores]


def test_experimental_marker_cannot_be_promoted_to_strict_frozen(frozen):
    store, receipt = frozen
    forged = receipt.model_copy(update={'hold_reasons': [marker()]})
    with pytest.raises(ValueError, match='must remain HOLD'):
        replay_module.replay(store, store.put(forged))


def test_duplicates_remain_invalid_outside_experimental_hold(frozen):
    store, receipt = frozen
    duplicate = receipt.model_copy(update={'assertion_packets': [*receipt.assertion_packets, receipt.assertion_packets[0]]})
    with pytest.raises(ValueError, match='Duplicate packet'):
        replay_module.replay(store, store.put(duplicate))


def test_same_digest_with_conflicting_reference_metadata_is_rejected(frozen):
    store, receipt = frozen
    candidate = held(receipt)
    changed = candidate.assertion_packets[0].model_copy(update={'relative_path': 'other/packet.json'})
    candidate = candidate.model_copy(update={'assertion_packets': [*candidate.assertion_packets, changed]})
    with pytest.raises(ValueError, match='Duplicate packet'):
        replay_module.replay(store, store.put(candidate))


def test_separate_reader_preserves_frozen_runtime_and_requires_round_authorization(frozen, tmp_path, monkeypatch):
    import copy
    import hashlib
    import json
    from pathlib import Path
    from types import SimpleNamespace
    from competition import scientific_lineage
    from competition.scientific_first_round import first_round_policy
    from scripts.scientific_replay_reader import install
    from validator.scientific_result_v1.source_tasks import errors
    from validator.scientific_result_v1.source_tasks.budgeted_validation import PROFILE
    from validator.scientific_result_v1.source_tasks.identity import runtime_descriptor
    store, receipt = frozen
    candidate = held(receipt)
    candidate = candidate.model_copy(update={'assertion_packets': [*candidate.assertion_packets, candidate.assertion_packets[0]]})
    ref = store.put(candidate)
    record = {'configuration_sha256': receipt.binding.policy_sha256, 'rules_public': {'configuration': {
        'scientific_scoring': {'first_round_scoring': first_round_policy('r1'),
            'experimental_launch': {'status': 'experimental'}, 'optimized_validation': dict(PROFILE)}}}}
    store.backend = SimpleNamespace(get_round=lambda _: record)
    directory = Path(errors.__file__).parent
    manifest = tmp_path / 'reader.json'
    manifest.write_text(json.dumps({name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in ['errors.py', 'replay.py']}))
    before = runtime_descriptor()
    original_codes = copy.deepcopy(errors.CODES)
    with monkeypatch.context() as patch:
        patch.setattr(scientific_lineage, 'replay', scientific_lineage.replay)
        install(directory=directory, manifest=manifest, round_id='r1')
        assert runtime_descriptor() == before and errors.CODES == original_codes
        assert scientific_lineage.replay(store, ref) == candidate
        for changes in ['missing_policy', 'wrong_policy_hash', 'wrong_round']:
            changed = copy.deepcopy(record)
            if changes == 'missing_policy':
                changed['rules_public']['configuration']['scientific_scoring'].pop('first_round_scoring')
            elif changes == 'wrong_policy_hash':
                changed['configuration_sha256'] = '0' * 64
            store.backend = SimpleNamespace(get_round=lambda _, value=changed: value)
            if changes == 'wrong_round':
                install(directory=directory, manifest=manifest, round_id='other-round')
            with pytest.raises(ValueError, match='authorized|approved'):
                scientific_lineage.replay(store, ref)
