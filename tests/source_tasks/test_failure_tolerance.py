from types import SimpleNamespace as NS
from fractions import Fraction
import pytest

from validator.scientific_result_v1.source_tasks import failure_tolerance as f


def fixture(events,*,gate=1,threshold='1/2'):
    frame=NS(tiles=[NS(tile_id='p1',table_uid=None),NS(tile_id='p2',table_uid=None),NS(tile_id='t1',table_uid='table')])
    selections=[NS(output_alias=None,stream='CONTENT_PROSE',selected_ids=['p1','p2']),
                NS(output_alias=None,stream='CONTENT_TABLE',selected_ids=['t1']),
                NS(output_alias=None,stream='ARCHIVE',selected_ids=['t1']),
                NS(output_alias='miner',stream='OUTPUT_AUDIT',selected_ids=['p1','p2'])]
    receipt=NS(deterministic_gates=[NS(output_alias='miner',gate_bit=gate)],paper_scores=[NS(output_alias='miner')],state='HOLD')
    return f.project(receipt=receipt,frame=frame,manifest=NS(selections=selections),events=events,tolerance=f.policy(threshold))


def event(kind,unit,status='COMPLETED',bit=1,**changes):
    return {'stage':'SCIENTIFIC_CASE','status':status,'key':kind+unit,'output_alias':'miner',
            'task_kind':kind,'unit_id':unit,'bit':bit,'errors':[],**changes}


def complete():
    return [event('CONTENT','p1'),event('CONTENT','p2'),event('CONTENT','t1'),event('ARCHIVE','t1'),
            event('OUTPUT_AUDIT','p1'),event('OUTPUT_AUDIT','p2')]


def test_all_resolved_matches_guide_score():
    score=fixture(complete())['scores'][0]
    assert score['conservative_score']==score['score_upper_bound']==1
    assert score['coverage_accepted']


def test_scientific_fail_counts_as_completed_not_infrastructure_failure():
    events=complete();events[-1]['bit']=0
    score=fixture(events)['scores'][0]
    assert score['conservative_score']==.5
    assert score['coverage_accepted']
    assert score['components']['P']['resolved_weight_fraction']==1


@pytest.mark.parametrize('category',['NETWORK','INVALID_JSON','TIMEOUT','SOURCE','EVALUATOR','UNEXPECTED_EXCEPTION'])
def test_every_failure_type_retains_unknown_in_denominator(category):
    events=complete();events[-1].update(status='UNRESOLVED',bit=None,errors=[{'category':category}])
    output=fixture(events);score=output['scores'][0]
    assert score['conservative_score']==.5 and score['score_upper_bound']==1
    assert score['coverage_accepted']
    assert score['components']['P']['resolved_weight_fraction']==.5
    assert output['failure_categories'][category]==1


def test_source_card_failure_does_not_erase_other_tiles_or_t_and_p():
    events=[e for e in complete() if not(e['task_kind']=='CONTENT' and e['unit_id']=='p1')]
    score=fixture(events)['scores'][0]
    # Missing whole tile is unknown, not absent from the C average.
    assert score['components']['C']['lower']==.75
    assert score['components']['C']['upper']==1
    assert score['components']['T']['lower']==score['components']['P']['lower']==1
    assert score['conservative_score']==pytest.approx(7.25/9)
    assert score['coverage_accepted']


def test_threshold_checked_per_component_not_globally():
    events=complete();events[-2]['status']=events[-1]['status']='UNRESOLVED'
    score=fixture(events)['scores'][0]
    assert not score['coverage_accepted']
    assert score['conservative_score']==0
    assert score['components']['C']['threshold_met']
    assert not score['components']['P']['threshold_met']


def test_all_failures_still_produce_numeric_bound_without_claiming_acceptance():
    score=fixture([])['scores'][0]
    assert score['conservative_score']==0 and score['score_upper_bound']==1
    assert not score['coverage_accepted']


def test_unknown_gate_cannot_earn_demonstrated_credit():
    score=fixture(complete(),gate=None)['scores'][0]
    assert score['conservative_score']==0 and score['score_upper_bound']==1
    assert not score['coverage_accepted']


def test_gate_failure_remains_zero_no_matter_tolerance():
    score=fixture([],gate=0)['scores'][0]
    assert score['conservative_score']==score['score_upper_bound']==0
    assert score['package_failure']


def test_threshold_is_exact_and_not_mutable_float_comparison():
    events=complete();events[-1]['status']='UNRESOLVED'
    assert fixture(events,threshold='1/2')['scores'][0]['coverage_accepted']
    assert not fixture(events,threshold='4/5')['scores'][0]['coverage_accepted']
    for bad in ['0','3/2','-1/2']:
        with pytest.raises(ValueError):f.policy(bad)


def test_eighty_percent_flag_does_not_change_any_numeric_score():
    events=complete();events[-1]['status']='UNRESOLVED'
    old=fixture(events,threshold='1/2')['scores'][0]
    new=fixture(events,threshold='4/5')['scores'][0]
    assert old['coverage_accepted'] and not new['coverage_accepted']
    assert old['conservative_score']==new['conservative_score']==.5
    assert old['score_upper_bound']==new['score_upper_bound']==1
