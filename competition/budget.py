"""Compute the frozen final size from the proposal's weekly cost curve."""

from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR


OVERHEAD = Decimal("20")
PRESCREEN_PAPERS = 20
SCREEN_PAPERS = 120
PRESCREEN_PACKAGE_COST = Decimal("0.096")
SCREEN_PACKAGE_COST = Decimal("0.105")
FINAL_PACKAGE_COST = Decimal("0.12")
FIXED_PAPER_COST = Decimal("0.07")
FINAL_COST_PER_PAPER = FIXED_PAPER_COST + 4 * FINAL_PACKAGE_COST


def final_paper_count(
    *, weekly_budget_usd: Decimal | int | float,
    distinct_challengers: int, week_one: bool,
) -> int:
    """Budget after a seeded duplicate draw; fail if a staged final is under 500."""
    if (isinstance(distinct_challengers, bool) or not isinstance(distinct_challengers, int)
        or distinct_challengers < 1):
        raise ValueError("final budget needs at least one distinct challenger")
    if isinstance(weekly_budget_usd, bool):
        raise ValueError("weekly budget must be positive")
    try:
        budget = Decimal(str(weekly_budget_usd))
    except (ValueError, ArithmeticError) as exc:
        raise ValueError("weekly budget is invalid") from exc
    if not budget.is_finite() or budget <= 0:
        raise ValueError("weekly budget must be positive")
    if week_one:
        return 320
    prescreen = (PRESCREEN_PAPERS *
                 ((distinct_challengers + 1) * PRESCREEN_PACKAGE_COST + FIXED_PAPER_COST)
                 if distinct_challengers > 12 else Decimal(0))
    screened = min(distinct_challengers, 12)
    screen = (SCREEN_PAPERS *
              ((screened + 1) * SCREEN_PACKAGE_COST + FIXED_PAPER_COST)
              if distinct_challengers > 3 else Decimal(0))
    count = int(((budget - OVERHEAD - prescreen - screen) / FINAL_COST_PER_PAPER)
                .to_integral_value(rounding=ROUND_FLOOR))
    if count < 500:
        raise ValueError("weekly budget cannot fund the 500-paper staged final")
    return count
