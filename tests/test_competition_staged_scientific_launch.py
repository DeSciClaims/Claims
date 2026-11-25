from types import SimpleNamespace
import hashlib
import pytest
from competition.round_evaluation import round_evaluation,stages_for_round
from competition.round_controller import reveal_selected_sample
from competition.sample_release import PaperRecord
from competition.drand import VerifiedBeacon,DEFAULT_CHAIN_HASH,DEFAULT_PUBLIC_KEY


def record():
    return dict(round_id='round',network='mainnet',status='locked',incumbent_hotkey=None,
        deadline_at='2026-11-25T15:00:00+00:00',baseline_package_sha256='a'*64,
        rules_public={'policy':{'validation_protocol':'claims-source-task-v1','winner_selection':'silver_tiebreak',
        'experimental_launch':{'status':'experimental'},'evaluation':{
        'schema_version':'claims_competition_evaluation_v1','mode':'production','required_challengers':1,
        'reference_baseline_stages':True,'stages':[
        {'name':'prescreen','papers':20,'advance_top':12,'max_per_team':2},
        {'name':'screen','papers':120,'advance_top':3,'max_per_team':1},{'name':'final','papers':500}]}}})


@pytest.mark.parametrize('entrants,expected',[(1,('final',)),(3,('final',)),
    (12,('screen','final')),(13,('prescreen','screen','final'))])
def test_staged_reference_preserves_small_field_rules(entrants,expected):
    row=record();assert stages_for_round(row,entrants)==expected
    assert row['incumbent_hotkey'] is None


@pytest.mark.parametrize('defect',['incumbent','claims','unannounced'])
def test_staged_launch_is_explicit_and_scientific_only(defect):
    row=record()
    if defect=='incumbent':row['incumbent_hotkey']='someone'
    elif defect=='claims':row['rules_public']['policy']['validation_protocol']='claims-v0'
    else:row['rules_public']['policy'].pop('experimental_launch')
    with pytest.raises(ValueError,match='Staged reference launch'):round_evaluation(row)


def test_reference_launch_releases_640_disjoint_stage_papers(monkeypatch):
    row=record()
    class Backend:
        def get_round(self,_):return row
        def list_submissions(self,_):return []
        def reveal_sample(self,_,receipt):self.receipt=receipt;return {'status':'revealed'}
    signature=b's'*96
    beacon=VerifiedBeacon(5,hashlib.sha256(signature).digest(),signature.hex(),DEFAULT_CHAIN_HASH,DEFAULT_PUBLIC_KEY,1800000000)
    monkeypatch.setattr('competition.round_controller.beacon_for_round',lambda *a,**k:beacon)
    monkeypatch.setattr('competition.round_controller._admitted_duplicate_selection',
        lambda *a,**k:SimpleNamespace(selected_hotkeys=tuple(str(i) for i in range(20))))
    backend=Backend()
    result=reveal_selected_sample('round',backend=backend,chain=object(),
        papers=[PaperRecord(str(i),f'{i:064x}') for i in range(700)],
        baseline={'package_sha256':'a'*64},package_for_submission=lambda _:b'package',weekly_budget_usd=1200)
    assert result['stages']==['prescreen','screen','final']
    assert [sum(p['stage']==s for p in backend.receipt['papers']) for s in result['stages']]==[20,120,500]
    assert len({p['paper_id'] for p in backend.receipt['papers']})==640
