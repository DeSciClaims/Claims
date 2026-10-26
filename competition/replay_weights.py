"""Build the second validator's 70/20/10 plan from its local crown verdict."""

from __future__ import annotations

from typing import Mapping

from .final_selection import FinalSelection
from .weights import Payee, WeightPlan, weekly_weight_plan


def replay_weight_plan(
    selection: FinalSelection, *, operator_hotkey: str,
    incumbent_hotkey: str | None, incumbent_team_id: str | None,
    qualified_by_hotkey: Mapping[str, bool], uid_by_hotkey: Mapping[str, int],
    burn_uid: int = 0,
) -> WeightPlan:
    """Allocate shares after a separately verified qualification snapshot.

    Qualification and metagraph UID resolution are explicit inputs so this
    pure function cannot silently borrow the primary validator's vector.
    """
    if bool(incumbent_hotkey) != bool(incumbent_team_id):
        raise ValueError("replay incumbent identity is incomplete")
    finalists = []
    for verdict in selection.verdicts:
        if (verdict.hotkey not in qualified_by_hotkey
            or type(qualified_by_hotkey[verdict.hotkey]) is not bool):
            raise ValueError("replay finalist qualification is missing")
        finalists.append(Payee(
            verdict.hotkey, verdict.team_id,
            qualified_by_hotkey[verdict.hotkey], verdict.mean_gain,
            copy=verdict.reason == "copy",
            code_passed=verdict.reason != "code_failed",
        ))
    production = None
    hotkey = selection.production_hotkey
    if hotkey is not None:
        production = next((row for row in finalists if row.hotkey == hotkey), None)
        if production is None:
            if (hotkey != incumbent_hotkey or incumbent_team_id is None
                or selection.winner_gain != 0):
                raise ValueError("replay production payee is not a frozen finalist or incumbent")
            if (hotkey not in qualified_by_hotkey
                or type(qualified_by_hotkey[hotkey]) is not bool):
                raise ValueError("replay incumbent qualification is missing")
            production = Payee(
                hotkey, incumbent_team_id,
                qualified_by_hotkey[hotkey],
            )
        elif not next(row for row in selection.verdicts if row.hotkey == hotkey).cleared:
            raise ValueError("replay production finalist did not clear the crown")
    return weekly_weight_plan(
        operator_hotkey=operator_hotkey, production=production,
        finalists=finalists, uid_by_hotkey=uid_by_hotkey,
        burn_uid=burn_uid, winner_gain=selection.winner_gain,
    )
