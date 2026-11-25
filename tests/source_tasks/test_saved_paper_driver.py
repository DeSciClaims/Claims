import pytest

from scripts.evaluate_scientific_saved_paper import select_models
from validator.scientific_result_v1.source_tasks.configuration import MODELS


def test_model_overrides_are_explicit_and_leave_original_roles_unchanged():
    original = dict(MODELS)
    selected = select_models(original, ['SOURCE_READER=minimax/minimax-m3.1-flash'])
    assert selected['SOURCE_READER'] == 'minimax/minimax-m3.1-flash'
    assert original == MODELS
    assert selected['SECONDARY'] == MODELS['SECONDARY']


@pytest.mark.parametrize('assignments', [
    ['SOURCE_READER=unapproved/model'],
    ['UNKNOWN=minimax/minimax-m3.1-flash'],
    ['SOURCE_READER'],
    ['SOURCE_READER=minimax/minimax-m3.1-flash', 'SOURCE_READER=qwen/qwen3.8-flash'],
])
def test_invalid_or_duplicate_model_overrides_are_rejected(assignments):
    with pytest.raises(ValueError):
        select_models(MODELS, assignments)
