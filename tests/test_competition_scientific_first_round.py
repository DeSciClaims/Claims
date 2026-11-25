import pytest
from competition.scientific_first_round import first_round_policy, configured_policy, validate_policy


def test_first_round_policy_is_explicit_round_bound_and_advisory():
    assert configured_policy({}, 'round-1') is None
    value=first_round_policy('round-1')
    config={'scientific_scoring':{'first_round_scoring':value}}
    assert configured_policy(config,'round-1')==value
    assert value['coverage_target']=='4/5'
    assert value['coverage_threshold_behavior']=='advisory'
    with pytest.raises(ValueError):configured_policy(config,'round-2')
    for changes in ({'coverage_target':'1/2'}, {'coverage_threshold_behavior':'veto'},
                    {'unresolved_credit':'one'}, {'retain_unresolved_denominators':False},
                    {'ranking_score':'upper_bound'}, {'extra':True}):
        with pytest.raises(ValueError):validate_policy({**value,**changes},'round-1')
