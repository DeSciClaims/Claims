import copy,json
from types import SimpleNamespace
import pytest
from competition import setup_timeout_review as review

LOG=b'''[venv] start: /runtime/python -m venv /work/venv
[venv] exit=0 elapsed=30.00s
[install] start: /work/venv/bin/python -m pip install --no-index --find-links /wheelhouse /input/miner-1-py3-none-any.whl
Installing collected packages: torch, docling, miner
[install] timeout after 570.00s
'''

def fixture():
    blobs={}
    def put(raw):
        if not isinstance(raw,bytes):raw=review.canonical(raw)
        digest=review.sha(raw);blobs[digest]=raw
        return dict(relative_path='blobs/'+digest,sha256=digest,byte_length=len(raw),media_type='application/json')
    original={'status':'build_failed','detail':'package install failed','artifact':None,
        'prompt_and_completion_tokens':0,'inference_cost_usd':0,'denied_egress_packets':0}
    job=dict(job_id='j',round_id='r',stage='final',hotkey='h',paper_id='p',package_sha256='a'*64,
        log_object_key='logs/j',status='completed',result_private=original)
    value=dict(kind=review.KIND,round_id='r',stage='final',configuration_sha256='c'*64,
        approved_by='owner',approved_at=123,preserve_paper_scores=True,
        verified_scientific_audit=put(dict(schema_version='claims_source_code_audit_v1',round_id='r',stage='final',scientific_lineage=[])),
        records=[{**{k:job[k] for k in ('job_id','hotkey','paper_id','package_sha256','log_object_key')},'original_result':copy.deepcopy(original),'log':put(LOG)}])
    record=dict(round_id='r',configuration_sha256='c'*64,rules_public={'policy':{'validation_protocol':'claims-source-task-v1','winner_selection':'silver_tiebreak'}})
    return SimpleNamespace(get_blob=blobs.__getitem__),put,value,record,[job]


def test_review_changes_only_whole_entry_build_eligibility_and_retains_failures():
    store,put,value,record,jobs=fixture()
    evidence={'packages':[{'hotkey':'h','source_fingerprint':'f'*64,'build_passed':False,
        'completed_job_count':429,'status_counts':{'completed':429,'build_failed':1}}]}
    before=copy.deepcopy((evidence,jobs));result=review.apply(evidence,store=store,ref=put(value),record=record,jobs=jobs)
    assert result['packages'][0]['build_passed'] is True
    assert result['packages'][0]['setup_timeout_count']==1
    assert result['packages'][0]['status_counts']==evidence['packages'][0]['status_counts']
    assert (evidence,jobs)==before
    evidence['packages'][0]['status_counts']['build_failed']=2
    assert review.apply(evidence,store=store,ref=put(value),record=record,jobs=jobs)['packages'][0]['build_passed'] is False
    evidence['packages'][0]['completed_job_count']=0
    assert review.apply(evidence,store=store,ref=put(value),record=record,jobs=jobs)['packages'][0]['build_passed'] is False


@pytest.mark.parametrize('tail',[b'[execute] start: miner\n',b'ERROR: incompatible dependency\n',b'[install] exit=1 elapsed=570.00s\n'])
def test_miner_execution_or_real_pip_error_is_not_waived(tail):
    with pytest.raises(ValueError):review.verified_timeout(LOG+tail)


def test_short_budget_source_build_or_early_failure_is_not_waived():
    for raw in [LOG.replace(b'570.00',b'20.00'),LOG.replace(b'.whl',b'.tar.gz'),LOG.replace(b'Installing collected packages:',b'Resolving dependencies:')]:
        with pytest.raises(ValueError):review.verified_timeout(raw)


@pytest.mark.parametrize('change',['package','job','result','scope','duplicate','log'])
def test_receipt_cannot_transfer_approval_to_other_jobs_or_changed_evidence(change):
    store,put,value,record,jobs=fixture()
    if change=='package':jobs[0]['package_sha256']='b'*64
    elif change=='job':jobs[0]['job_id']='other'
    elif change=='result':jobs[0]['result_private']['prompt_and_completion_tokens']=1
    elif change=='scope':record['configuration_sha256']='d'*64
    elif change=='duplicate':value['records']*=2
    elif change=='log':value['records'][0]['log']['byte_length']+=1
    with pytest.raises(ValueError):review.validate(store,put(value),record,jobs)
