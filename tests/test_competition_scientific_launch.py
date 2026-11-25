from copy import deepcopy
import pytest
from competition.scientific_launch import pilot_report, validate_launch, SCHEMA
from validator.scientific_result_v1.source_tasks.qualification import activation_failures


def fixture():
    assessment={'kind':'claims_testnet_conservative_assessment_v1','round_id':'round_testnet',
        'policy':{'coverage_threshold_behavior':'advisory'},
        'papers':[{'stage':'week_one','paper_id':str(i)} for i in range(10)],
        'scores':[{'stage':'week_one','paper_id':str(i),'hotkey':str(k),'package_sha256':str(k)} for i in range(10) for k in range(3)]}
    value={'schema_version':SCHEMA,'round_id':'round_mainnet','basis_round_id':'round_testnet',
        'basis_assessment':{'sha256':'a'*64},'scientific_accuracy_policy':'unmeasured_without_truth_corpus',
        'unmet_qualification_requirements':activation_failures(pilot_report(assessment),truth_corpus_required=False)}
    return value,assessment


def test_experimental_launch_discloses_missing_qualification_without_claiming_it_passed():
    value,assessment=fixture()
    assert validate_launch(value,'round_mainnet',assessment)==value
    assert '50-paper, three-pipeline operational pilot incomplete' in value['unmet_qualification_requirements']
    assert 'Ten-paper P census/resampling diagnostic incomplete' in value['unmet_qualification_requirements']


@pytest.mark.parametrize('defect',['round','kind','basis','missing_score','duplicate_score','small_pilot','hidden_requirements','accuracy'])
def test_experimental_launch_cannot_hide_requirements_or_invent_completed_evidence(defect):
    value,assessment=fixture();value=deepcopy(value)
    if defect=='round':value['round_id']='other'
    if defect=='kind':assessment['kind']='unknown'
    if defect=='basis':value['basis_round_id']='other_testnet'
    if defect=='missing_score':assessment['scores'].pop()
    if defect=='duplicate_score':assessment['scores'].append(assessment['scores'][0])
    if defect=='small_pilot':assessment['papers'].pop()
    if defect=='hidden_requirements':value['unmet_qualification_requirements']=[]
    if defect=='accuracy':value['scientific_accuracy_policy']='qualified'
    with pytest.raises(ValueError):validate_launch(value,'round_mainnet',assessment)
