import pytest

from competition.scientific_eligibility import required_eligible_papers


def config(floors=None):
    return {'scientific_scoring':{'recovery':{'eligibility':{'schema_version':'claims_common_eligibility_v1',
        'minimum_fraction':{'numerator':1,'denominator':2},'minimum_papers_by_stage':floors or {'week_one':1}}}}}


@pytest.mark.parametrize('planned,expected',[(1,1),(2,1),(5,3),(10,5),(11,6)])
def test_half_sample_rounds_up(planned,expected):
    assert required_eligible_papers(config(),'testnet','week_one',planned)==expected


def test_historical_rule_unchanged():
    assert required_eligible_papers({},'testnet','week_one',10)==10


def test_mainnet_absolute_minimum_and_invalid_plans():
    assert required_eligible_papers(config({'final':500}),'mainnet','final',800)==500
    assert required_eligible_papers(config({'final':500}),'mainnet','final',1200)==600
    with pytest.raises(ValueError):required_eligible_papers(config({'final':5}),'mainnet','final',10)
    with pytest.raises(ValueError):required_eligible_papers(config({'final':500}),'mainnet','final',499)


@pytest.mark.parametrize('fraction',[{'numerator':True,'denominator':2},{'numerator':1,'denominator':0},{'numerator':3,'denominator':2}])
def test_invalid_fraction_rejected(fraction):
    c=config();c['scientific_scoring']['recovery']['eligibility']['minimum_fraction']=fraction
    with pytest.raises(ValueError):required_eligible_papers(c,'testnet','week_one',10)
