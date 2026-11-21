from validator.scientific_result_v1.source_tasks.qualification import accuracy,activation_failures,wilson
from validator.scientific_result_v1.source_tasks.diagnostics import diagnostic
from validator.scientific_result_v1.source_tasks.sampling import PROTOCOL


def test_missing_qualification_is_not_an_approval_and_holds_stay_in_denominator():
    report=accuracy([{'case_id':'p','positive':True,'outcome':'HOLD'},{'case_id':'n','positive':False,'outcome':'PASS'}],[])
    assert not report['qualified']
    assert report['counts']['positive_fail_or_hold']==1 and report['counts']['negative_pass']==1
    assert report['metrics'][2]['denominator']==2
    assert activation_failures({})
    assert wilson(0,0) is None and wilson(0,100)[1]>0


def test_diagnostic_fixed_census_and_shared_nested_resamples_preserve_ties():
    output={'gate':1,'content':1,'archive':1,'content_available':True,'archive_available':True,'bucket_bits':{'b1':1,'b2':0},'unanchored_required':False,'unanchored_bit':None}
    papers=[{'paper_id':'p','eligible':True,'scope':(PROTOCOL,'r','final','p'),'outputs':{'a':dict(output),'b':dict(output)}}]
    result=diagnostic(papers,pipeline_ids=['a','b'],secret=b'x'*32,pilot_manifest_sha256='1'*64)
    assert result['complete_paired_papers']==1 and result['incomplete_paired_papers']==0
    assert result['census_paired_gain']=={'numerator':0,'denominator':1}
    for row in result['results'].values():
        assert len(row['replicates'])==2000 and row['order_reversal_rate'] is None
        assert row['sampled_tie_count']==2000 and row['gain_errors']['mae']==0
