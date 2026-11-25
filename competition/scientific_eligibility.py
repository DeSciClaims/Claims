"""Frozen common-paper minima. Old rounds retain their original full-sample rule."""
from fractions import Fraction

PRODUCTION_MINIMUMS = {'prescreen':20,'screen':120,'final':500,'week_one':320}


def required_eligible_papers(configuration, network, stage, planned):
    if type(planned) is not int or planned < 1 or stage not in PRODUCTION_MINIMUMS:
        raise ValueError('Invalid scientific stage or planned sample')
    raw=configuration.get('scientific_scoring',{}).get('recovery',{}).get('eligibility')
    if raw is None:return planned
    scoped=isinstance(raw,dict) and raw.get('schema_version')=='claims_common_eligibility_v2'
    if (not isinstance(raw,dict) or set(raw)!=({'schema_version','minimum_fraction','minimum_papers_by_stage'}|({'round_id'} if scoped else set()))
        or raw['schema_version'] not in {'claims_common_eligibility_v1','claims_common_eligibility_v2'}):
        raise ValueError('Invalid frozen common eligibility policy')
    if scoped:
        spec=configuration.get('scientific_scoring',{});advisory=spec.get('first_round_scoring',{})
        if (network!='mainnet' or advisory.get('kind')!='claims_first_round_conservative_v1'
            or advisory.get('round_id')!=raw['round_id'] or spec.get('experimental_launch',{}).get('round_id')!=raw['round_id']
            or raw['minimum_fraction']!={'numerator':1,'denominator':2}):
            raise ValueError('Reduced mainnet paper minima require round-bound experimental advisory approval')
    fraction=raw['minimum_fraction']
    if (not isinstance(fraction,dict) or set(fraction)!={'numerator','denominator'}
        or any(type(fraction[k]) is not int for k in fraction)
        or not 1<=fraction['numerator']<=fraction['denominator']):
        raise ValueError('Invalid common eligibility fraction')
    floors=raw['minimum_papers_by_stage']
    if not isinstance(floors,dict) or stage not in floors or set(floors)-PRODUCTION_MINIMUMS.keys():
        raise ValueError('Missing frozen absolute stage minimum')
    for key,value in floors.items():
        minimum=((PRODUCTION_MINIMUMS[key]+1)//2 if scoped else PRODUCTION_MINIMUMS[key]) if network=='mainnet' else 1
        if type(value) is not int or value < minimum:
            raise ValueError('Absolute stage minimum violates the competition policy')
    share=Fraction(fraction['numerator'],fraction['denominator'])
    required=max((planned*share.numerator+share.denominator-1)//share.denominator,floors[stage])
    if required>planned:raise ValueError('Planned sample cannot meet its absolute minimum')
    return required
