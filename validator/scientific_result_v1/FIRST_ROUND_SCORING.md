# First-round scoring with incomplete validation

For the first round, every miner receives a conservative numeric score even when
the validator cannot finish all its checks. The evaluation completion target is
**80% separately in each applicable C, T and P component**. It is an advisory flag,
not a requirement for receiving a score or entering the ranking.

| Outcome | Score treatment | Recorded as |
|---|---|---|
| Supported scientific check | Credit under the existing C/T/P formula | Resolved PASS |
| Scientifically incorrect check | Zero credit | Resolved FAIL |
| Network error, invalid response, timeout, evidence or other evaluator failure | Zero demonstrated credit; retain its weight in the denominator | Unresolved, with the actual error |
| Confirmed mechanical failure | G=0, Q=0 | Mechanical failure |
| Mechanical gate could not be established | Conservative Q=0; upper bound may be greater than zero | Unknown gate, not a confirmed miner failure |

For scientific content and numerical tables, the ranking score is
**Q_lower = G × P_lower × (7C_lower + 2T_lower) / 9**.
Without qualifying tables it is **G × P_lower × C_lower**; table-only sources use
**G × P_lower × T_lower**. Unknown checks receive zero in each lower bound. The
upper bound assumes those unknown checks pass. Neither bound drops unresolved
regions, assertion buckets or table regions from the sampled denominator.

For example, if C=0.6, T=0.9 and P=0.8 after giving unknown work zero credit,
and G=1, the score is **0.5333**. If only 60% of C was successfully evaluated,
that same score still counts; its C completion flag is below target. A valid
scientific FAIL counts toward completion. Evaluation completion is not accuracy,
extraction recall or the percentage of source findings captured.

The record includes per-miner component bounds, weighted completion, resolved and
planned units, mechanical status and scientific PASS/FAIL/unresolved case counts.
Paper-level records retain stage completion, logical operation completion, failed
attempt categories and references to the original model requests and responses.
Failed attempts remain recorded even if a later attempt succeeds. These are
different denominators and should not be added together.

Strict guide receipts retain HOLD and null strict Q when incomplete. A separate,
hash-bound first-round score receipt authorizes the conservative ranking score.
Replay checks the original receipt, sample, verdicts, projection and frozen policy.
The 80% flag never rewrites the score to zero, excludes the paper or marks the
strict receipt fully verified. All entrants, including the baseline, retain the
same paper grid. Existing winner, incumbent-margin, team and executable-copy
rules continue to apply. The separate common-paper minimum is unchanged.
An unknown incumbent gate or evaluator infrastructure fault does not count as a
confirmed incumbent package failure; its conservative zero still counts in scores.

## Activation

The frozen round configuration must explicitly contain
`scientific_scoring.first_round_scoring` with this exact descriptor:

```json
{
  "kind": "claims_first_round_conservative_v1",
  "round_id": "REPLACE_WITH_THE_ACTUAL_FIRST_ROUND_ID",
  "coverage_target": "4/5",
  "coverage_threshold_behavior": "advisory",
  "unresolved_credit": "zero",
  "retain_unresolved_denominators": true,
  "ranking_score": "conservative_lower_bound"
}
```

`competition.scientific_first_round.first_round_policy(round_id)` constructs this
descriptor. Freeze it before the round, along with the runtime implementation
hashes. A different round ID is rejected. Configurations without it continue to
use the strict guide policy. Earlier rounds, requests, scores and weights are not
rewritten by adding this implementation.

The factory enables continuation, the pool retains completion evidence, and the
competition adapter supplies numeric paper/pair scores and uncertainty metadata.
Finalization verifies the separate score receipt against that round's policy.
The backend API needs its matching `20261124_first_round_scoring` migration and
code before activation. The migration accepts the separate receipt atomically and
retains the same score/component lineage in evaluation exports.
This policy accepts imperfect evaluation for the first round; it does not certify
scientific judge accuracy or change the miner's execution budget.
