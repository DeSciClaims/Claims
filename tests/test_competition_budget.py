from decimal import Decimal

import pytest

from competition.budget import final_paper_count


@pytest.mark.parametrize(("entrants", "expected"), [
    (1, 1000), (2, 1000), (3, 1218), (9, 973), (12, 905),
    (20, 829), (30, 794),
])
def test_final_count_uses_distinct_challengers_after_duplicate_selection(entrants, expected):
    assert final_paper_count(
        weekly_budget_usd=Decimal("690"), distinct_challengers=entrants,
        week_one=False,
    ) == expected


def test_week_one_and_unfunded_final():
    assert final_paper_count(
        weekly_budget_usd=445, distinct_challengers=10, week_one=True,
    ) == 320
    with pytest.raises(ValueError, match="500-paper"):
        final_paper_count(
            weekly_budget_usd=460, distinct_challengers=20, week_one=False,
        )
