import hashlib
import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from competition.duplicate_review import make_duplicate_review
from neurons.competition_duplicate_automation import prepare_review, prepare_bound_round


def wheel(source, resource=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('pipeline.py', source)
        if resource is not None:
            archive.writestr('prompt.txt', resource)
    return stream.getvalue()


@pytest.fixture
def setup(tmp_path):
    base = wheel('def common(): pass')
    (tmp_path / 'base.zip').write_bytes(base)
    policy = {'schema_version': 'claims_duplicate_automation_v1', 'enabled': True,
              'review_similarity_threshold': 0.8,
              'public_base_archives': [{'filename': 'base.zip', 'sha256': hashlib.sha256(base).hexdigest()}]}
    policy_path = tmp_path / 'policy.json'
    policy_path.write_text(json.dumps(policy))
    packages = {}
    rows = []
    states = []
    reads = []
    baseline = {'hotkey': 'reference', 'team_id': 'reference', 'package_sha256': 'a' * 64}
    record = {'round_id': 'round', 'network': 'testnet', 'status': 'locked',
              'configuration_sha256': 'c' * 64, 'baseline_package_sha256': 'a' * 64,
              'incumbent_hotkey': None, 'archive_block_hash': '0x' + 'd' * 64}
    document = {'round_id': 'round', 'configuration_sha256': 'c' * 64, 'baseline_upload': baseline}

    def add(hotkey, body):
        digest = hashlib.sha256(body).hexdigest()
        packages[digest] = body
        rows.append({'hotkey': hotkey, 'team_id': 'team-' + hotkey, 'package_sha256': digest,
                     'status': 'accepted', 'qualified': True})

    def read(receipt):
        reads.append(receipt['hotkey'])
        return packages[receipt['package_sha256']]

    backend = SimpleNamespace(list_submissions=lambda _: rows,
        runtime_request=lambda rid, suffix, state: states.append(dict(state)))
    def run():
        return prepare_review(record, backend, policy_path=policy_path, directory=tmp_path,
                              document=document, storage=SimpleNamespace(package_for_submission=read))
    return SimpleNamespace(root=tmp_path, policy=policy, policy_path=policy_path, states=states,
        rows=rows, record=record, document=document, packages=packages, reads=reads, add=add, run=run)


def test_clear_comparisons_generate_verified_receipt_and_reuse_it(setup):
    s = setup
    s.add('a', wheel('def extract(paper): return [x for x in paper if x.valid]'))
    s.add('b', wheel('class Parser:\n def run(self):\n  try: return dict(a=1)\n  except ValueError: return None'))
    assert s.run() == {'status': 'evaluation_ready'}
    review = json.loads((s.root / 'round.duplicates.json').read_text())
    assert review['links'] == [] and review['automation']['policy_sha256']
    assert s.states[-1]['status'] == 'complete' and s.states[-1]['decision'] == 'automatic'
    assert s.states[-1]['comparison_count'] == 1
    assert (s.root / 'round.duplicates.json').stat().st_mode & 0o777 == 0o600
    s.reads.clear()
    # Backend team merging must not alter the signed declaration snapshot.
    s.rows[1].update(team_id='team-a', declared_team_id='team-b')
    assert s.run() == {'status': 'evaluation_ready'}
    assert s.reads == []


def test_normalized_near_copy_waits_then_resumes_after_operator_review(setup):
    s = setup
    s.add('a', wheel('def extract(paper): return [x for x in paper if x.valid]'))
    s.add('b', wheel('def harvest(document): return [item for item in document if item.ok]'))
    assert s.run() == {'status': 'waiting_for_duplicate_review'}
    assert s.states[-1]['review_pair_count'] == 1
    assert not (s.root / 'round.duplicates.json').exists()
    report = json.loads((s.root / 'round.duplicate-candidates.json').read_text())
    review = make_duplicate_review('round', round_record=s.record, submissions=s.rows,
                                  linked_pairs=[], candidate_report=report)
    (s.root / 'round.duplicates.json').write_text(json.dumps(review))
    s.reads.clear()
    assert s.run() == {'status': 'evaluation_ready'}
    assert s.states[-1]['decision'] == 'operator' and s.reads == []


@pytest.mark.parametrize('identical_bytes', [True, False])
def test_existing_exact_hash_and_ast_rules_need_no_manual_review(setup, identical_bytes):
    s = setup
    body = wheel('def extract(paper): return paper')
    s.add('a', body)
    s.add('b', body if identical_bytes else wheel('# formatting only\ndef extract(paper):\n return paper\n'))
    assert s.run() == {'status': 'evaluation_ready'}
    assert s.states[-1]['review_pair_count'] == 0
    # The existing seeded duplicate selector still decides the representative.
    assert json.loads((s.root / 'round.duplicates.json').read_text())['links'] == []


def test_changed_prompt_is_not_assumed_to_be_an_exact_pipeline(setup):
    s = setup
    s.add('a', wheel('def extract(paper): return paper', 'First prompt'))
    s.add('b', wheel('def extract(paper): return paper', 'Different prompt'))
    assert s.run() == {'status': 'waiting_for_duplicate_review'}


def test_scan_errors_require_review_without_importing_package_code(setup):
    s = setup
    s.add('a', wheel('this is not valid python !!!'))
    s.add('b', wheel('raise AssertionError("must never execute miner code")'))
    assert s.run() == {'status': 'waiting_for_duplicate_review'}
    assert s.states[-1]['scan_error_count'] == 1
    assert not (s.root / 'round.duplicates.json').exists()


def test_public_base_checksum_failure_never_approves_a_package(setup):
    s = setup
    s.add('a', wheel('def extract(paper): return paper'))
    (s.root / 'base.zip').write_bytes(b'altered')
    with pytest.raises(ValueError, match='checksum'):
        s.run()
    assert s.states[-1]['status'] == 'error'
    assert not (s.root / 'round.duplicates.json').exists()


def test_waiting_for_predecessor_never_opens_storage_or_starts_scanning(monkeypatch):
    def forbidden(**kwargs):
        raise AssertionError('storage opened before predecessor was ready')
    monkeypatch.setattr('neurons.competition_duplicate_automation.hippius_s3_client', forbidden)
    factory = SimpleNamespace(prepare_evaluation=lambda *_: {'status': 'waiting_for_preceding_handoff'})
    assert prepare_bound_round({}, object(), factory, object()) == {'status': 'waiting_for_preceding_handoff'}


def test_policy_is_frozen_for_a_round_while_later_rounds_can_change(setup):
    s = setup
    s.add('a', wheel('def extract(paper): return [x for x in paper if x.valid]'))
    s.add('b', wheel('def harvest(document): return [item for item in document if item.ok]'))
    assert s.run()['status'] == 'waiting_for_duplicate_review'
    original = s.states[-1]['policy_sha256']
    s.policy['review_similarity_threshold'] = 1
    s.policy_path.write_text(json.dumps(s.policy))
    assert s.run()['status'] == 'waiting_for_duplicate_review'
    assert s.states[-1]['policy_sha256'] == original


def test_bound_incumbent_is_compared_to_challengers(setup):
    s = setup
    s.add('a', wheel('def extract(paper): return [x for x in paper if x.valid]'))
    s.add('incumbent', wheel('def harvest(document): return [item for item in document if item.ok]'))
    incumbent = s.rows.pop()
    s.record.update(incumbent_hotkey='incumbent', baseline_package_sha256=incumbent['package_sha256'])
    s.document['baseline_upload'] = incumbent
    assert s.run()['status'] == 'waiting_for_duplicate_review'
    assert s.states[-1]['comparison_count'] == 1
    assert s.states[-1]['package_count'] == 2


def test_report_or_review_cannot_be_reused_for_a_different_intake(setup):
    s = setup
    s.add('a', wheel('def extract(paper): return paper'))
    assert s.run()['status'] == 'evaluation_ready'
    s.add('b', wheel('class Other: pass'))
    with pytest.raises(ValueError):
        s.run()
    assert s.states[-1]['status'] == 'error'
