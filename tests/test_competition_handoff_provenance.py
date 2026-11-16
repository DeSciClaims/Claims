import copy
from types import SimpleNamespace

import pytest

from competition.handoff_provenance import verify_preserved_scores
from competition.production_handoff import planned_production_handoff
from competition.publication import canonical_sha256, sign_publication
from test_competition_production_handoff import _fixture
from datetime import UTC, datetime


def provenance_fixture():
    signer, record, stage, receipt = _fixture()
    original = {**record['rules_public'], 'configuration': {'model': 'original'}}
    original['configuration_sha256'] = canonical_sha256(original['configuration'])
    snapshots = {1: original}
    amendments = []
    for number in (2, 3):
        current = {**original, 'configuration': {'model': 'version-' + str(number)}}
        current['configuration_sha256'] = canonical_sha256(current['configuration'])
        before = snapshots[number-1]
        amendments.append({'round_id': 'week2', 'from_revision': number-1, 'to_revision': number,
            'previous_rules_sha256': canonical_sha256(before), 'rules_sha256': canonical_sha256(current),
            'previous_configuration_sha256': before['configuration_sha256'],
            'configuration_sha256': current['configuration_sha256'], 'resume_mode': 'resume',
            'restart_stage': None, 'preserved_paper_count': 1, 'amendment_sha256': str(number)*64})
        snapshots[number] = current
    record.update(rules_public=snapshots[3], rules_sha256=canonical_sha256(snapshots[3]),
                  configuration_sha256=snapshots[3]['configuration_sha256'],
                  rules_revision=3, rules_revision_sha256='3'*64)
    bundle=copy.deepcopy(receipt['bundle'])
    bundle.update(rules_sha256=record['rules_sha256'], configuration_sha256=record['configuration_sha256'],
                  rules_revision=3, rules_revision_sha256='3'*64)
    rows=bundle['results']['final_pair_scores']
    rows[0]['configuration_sha256']=original['configuration_sha256']
    paper={'round_id':'week2','stage':'final','paper_id':'paper-1','input_sha256':'f'*64,
        'produced_rules_revision':1,'configuration_sha256':original['configuration_sha256'],
        'carried_forward': {'from_revision':1,'to_revision':3,'input_sha256':'f'*64,
                           'configuration_sha256':original['configuration_sha256']}}
    backend=SimpleNamespace(rules_amendments=lambda _: {'rules_revision':3,'amendments':amendments},
        rules_snapshot=lambda _, revision: snapshots[revision], list_evaluation_provenance=lambda *_:[paper])
    return signer,record,stage,bundle,backend,paper,amendments,snapshots


def test_handoff_accepts_signed_preserved_scores_and_keeps_exact_package_checks():
    signer,record,stage,bundle,backend,*_=provenance_fixture()
    receipt=sign_publication(bundle,signer)
    args=dict(receipt=receipt, round_record=record, final_stage=stage,
        expected_signer_hotkey=signer.ss58_address, network='testnet',now=datetime(2026,11,9,9,tzinfo=UTC))
    with pytest.raises(ValueError, match='provenance'):
        planned_production_handoff(**args)
    plan=planned_production_handoff(**args,backend=backend)
    assert plan.package_sha256==stage['entries'][0]['package_sha256']
    assert plan.receipt_sha256==receipt['bundle_sha256']
    stage['entries'][0]['package_sha256']='7'*64
    with pytest.raises(ValueError,match='signed final paper grid'):
        planned_production_handoff(**args,backend=backend)


@pytest.mark.parametrize('fault',['missing_paper','foreign_paper','wrong_origin','wrong_input','wrong_target',
    'unknown_configuration','broken_chain','restart','wrong_amendment_digest','wrong_snapshot'])
def test_handoff_rejects_unproved_or_tampered_score_provenance(fault):
    _,record,_,bundle,backend,paper,amendments,snapshots=provenance_fixture()
    if fault=='missing_paper': backend.list_evaluation_provenance=lambda *_:[]
    if fault=='foreign_paper': paper['round_id']='another-round'
    if fault=='wrong_origin': paper['produced_rules_revision']=2
    if fault=='wrong_input': paper['carried_forward']['input_sha256']='0'*64
    if fault=='wrong_target': paper['carried_forward']['to_revision']=2
    if fault=='unknown_configuration': bundle['results']['final_pair_scores'][0]['configuration_sha256']='0'*64
    if fault=='broken_chain': amendments[0]['previous_rules_sha256']='0'*64
    if fault=='restart': amendments[0]['resume_mode']='restart_stage';amendments[0]['restart_stage']='final'
    if fault=='wrong_amendment_digest': amendments[-1]['amendment_sha256']='0'*64
    if fault=='wrong_snapshot': snapshots[1]['configuration']['model']='tampered'
    with pytest.raises(ValueError):
        verify_preserved_scores(backend,record,bundle,bundle['results']['final_pair_scores'])
