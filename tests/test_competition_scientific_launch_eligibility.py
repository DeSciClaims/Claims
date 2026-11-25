import pytest
from competition.scientific_eligibility import required_eligible_papers


def configuration():
    return {'scientific_scoring':{'first_round_scoring':{'kind':'claims_first_round_conservative_v1','round_id':'round'},
        'experimental_launch':{'round_id':'round'},'recovery':{'eligibility':{
        'schema_version':'claims_common_eligibility_v2','round_id':'round',
        'minimum_fraction':{'numerator':1,'denominator':2},
        'minimum_papers_by_stage':{'prescreen':10,'screen':60,'final':250}}}}}


@pytest.mark.parametrize('stage,planned,required',[('prescreen',20,10),('screen',120,60),('final',500,250),('final',501,251)])
def test_scoped_launch_uses_owner_approved_half_sample(stage,planned,required):
    assert required_eligible_papers(configuration(),'mainnet',stage,planned)==required


@pytest.mark.parametrize('defect',['round','advisory','fraction','floor'])
def test_reduced_minimum_cannot_leak_into_other_mainnet_rounds(defect):
    config=configuration();spec=config['scientific_scoring'];policy=spec['recovery']['eligibility']
    if defect=='round':policy['round_id']='different'
    elif defect=='advisory':spec.pop('first_round_scoring')
    elif defect=='fraction':policy['minimum_fraction']={'numerator':1,'denominator':10}
    else:policy['minimum_papers_by_stage']['final']=249
    with pytest.raises(ValueError):required_eligible_papers(config,'mainnet','final',500)
