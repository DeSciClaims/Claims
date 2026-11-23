"""Frozen common-paper minima. Old rounds retain their original full-sample rule."""
from fractions import Fraction

PRODUCTION_MINIMUMS = {'prescreen':20,'screen':120,'final':500,'week_one':320}


def required_eligible_papers(configuration, network, stage, planned):
    if type(planned) is not int or planned < 1 or stage not in PRODUCTION_MINIMUMS:
        raise ValueError('Invalid scientific stage or planned sample')
    raw=configuration.get('scientific_scoring',{}).get('recovery',{}).get('eligibility')
    if raw is None:return planned
    if (not isinstance(raw,dict) or set(raw)!={'schema_version','minimum_fraction','minimum_papers_by_stage'}
        or raw['schema_version']!='claims_common_eligibility_v1'):
        raise ValueError('Invalid frozen common eligibility policy')
    fraction=raw['minimum_fraction']
    if (not isinstance(fraction,dict) or set(fraction)!={'numerator','denominator'}
        or any(type(fraction[k]) is not int for k in fraction)
        or not 1<=fraction['numerator']<=fraction['denominator']):
        raise ValueError('Invalid common eligibility fraction')
    floors=raw['minimum_papers_by_stage']
    if not isinstance(floors,dict) or stage not in floors or set(floors)-PRODUCTION_MINIMUMS.keys():
        raise ValueError('Missing frozen absolute stage minimum')
    for key,value in floors.items():
        if type(value) is not int or value < (PRODUCTION_MINIMUMS[key] if network=='mainnet' else 1):
            raise ValueError('Absolute stage minimum violates the competition policy')
    share=Fraction(fraction['numerator'],fraction['denominator'])
    required=max((planned*share.numerator+share.denominator-1)//share.denominator,floors[stage])
    if required>planned:raise ValueError('Planned sample cannot meet its absolute minimum')
    return required
