# Decision validation mode

Set `CLAIMS_ELIGIBILITY_OUTPUT_CONTRACT=decision` to select the decision workflow.
`compact` remains the environment default; `verbose` and the existing compact path
remain selectable. No deployment or production environment change is included.

Decision mode uses schema-constrained OpenAI-compatible provider requests (currently
DeepSeek through OpenRouter), not an OpenAI Decisions or Jev provider integration.
Comparison returns relation/confidence classifications for retrieved pairs. Code
constructs complete-link equivalence groups; unclassified pair chains do not grant
bypass. Eligibility judges independently decide each candidate, so both members of
a pair can qualify. Decision mode constructs one-candidate cases: both members of a formerly paired case can pass as separate decisions. Each consensus record retains the existing selected-candidate-or-neither semantics, plus per-candidate primary decisions. Historical selection records remain unchanged.

One source-only reconstruction is generated lazily for a paper with disagreements.
It contains no candidate claims or primary votes, is hashed and saved, and is reused
across every appellate packet in that paper execution. No stage-resume cache is
used by this mode; a new execution reconstructs from its own source packet.

Canonical draft outputs bounded flat candidate assignments, selecting existing
claim anchors and importance categories. The global audit returns approval or
specific corrections. Code validates the final partition and preserves source
provenance and group-specific shared coverage. Unit metadata stores description
claims separately from contributors. Existing `statement` consumers receive a
selected member statement, not synthesized wording. Technical output failures
cannot silently admit candidates or publish incomplete canonical partitions.

Diagnostics classify evidence/relevance and rigor issue categories without generated
reasons. Deterministic structural and grounding checks remain. This is a scientific
behavior change and requires source-grounded quality review, not just passing tests.

Configuration:

```env
CLAIMS_ELIGIBILITY_OUTPUT_CONTRACT=decision
CLAIMS_DECISION_BATCH_SIZE=24
CLAIMS_DECISION_MAX_TOKENS=4096
CLAIMS_DECISION_BLIND_MAX_TOKENS=16384
CLAIMS_DECISION_REASONING_ENABLED=false
CLAIMS_ELIGIBILITY_SAMPLE_VERBOSE_PAPER=true
```

Reasoning settings are sent to compatible OpenRouter routes. Unsupported required
parameters fail explicitly; do not silently switch models or strip settings.
Reference generation and the verbose sample have separate budgets. Always retain a
combined operational total as well. The 10k–20k routine output target is experimental.

## Paired testnet replay

```bash
python -m scripts.compare_decision_mode \
  --manifest /path/to/saved/manifest.json \
  --output-dir /path/to/new-results \
  --env-file /path/to/test-provider.env --limit 3
```

`--validate-only` checks saved source and candidate references without provider calls.
Arms alternate order across papers. Extraction/reference inputs are frozen; both
arms run fresh diagnostics, comparison, eligibility and canonicalization. DSPy caches
and stage resume are disabled. There are no backend clients, wallets or weight writes.
The verbose sample is disabled in this experiment to compare routine paths directly.

Exports include stage usage (failed attempts too), raw decision provider responses,
DSPy provider-counter audits, Silver membership, final scores/ranks and scores with
normal diagnostic findings held empty in both arms. That controlled result isolates
Silver/adjudication changes; it does not represent final deployed miner rewards.
Three small fixtures cannot establish full-batch ranking stability or scientific
quality. Single-miner fixtures have no meaningful within-paper ranking comparison.

## Usage accounting

Completion counters represent all provider-reported output, not just final JSON.
Reasoning is a breakdown where available; do not add it again to OpenAI-compatible
completion counts when inferring a missing total. The generic normalizer previously
did so; this branch fixes that fallback. Provider totals already present are retained.
DSPy nested Pydantic usage details are now recovered, and provider-reported cost is
preferred to a separate LM-history estimate. Missing usage remains unknown, never zero.
The deployed Hermes CanonicalUsage output count already includes reasoning; its total is prompt plus output. Claims previously added reasoning a second time when reconstructing session totals; this branch also corrects that total-only error. Hermes session counters include agent turns/tool calls; separate raw session counter
audits preserve the source of these totals. A small final JSON alone does not prove
low model output or low provider billing.

Decision cases are single-candidate records to preserve existing consensus answer construction. Verify materialization/preparation with these records before rollout. Replay has no consensus writes. Compare original source support, admissions, unit grouping,
importance and scores before enabling decision mode outside isolated tests.

## Initial live results (2026-10-21)

Three saved testnet papers (40 candidates, one/two/one miners) completed in both
modes. Corrected decision arms recorded 6,067 output tokens versus 301,014 for
fresh compact controls; total tokens were 440,539 versus 2,941,019. This comparison
bundles bounded contracts, batching, agent-loop removal and reasoning settings.
It is not an isolated schema-only effect or a full-size batch estimate.

Scientific outcomes changed: 13 of 23 jointly adjudicated candidates changed
admission, and the two-miner fixture reversed ranks. Keep compact as the default
pending source-grounded review. The supplementary vault report is
`Claims Team/Outputs/Decision mode versus compact validation 2026-10-21.md`.
It includes stage totals, score differences, fixture hashes and exact replay pins.

All 48 usage events reconcile to upstream output counters. Six compact DSPy
responses report reasoning greater than completion upstream; the analyzer exposes
this inconsistency without altering the provider counters. A separate source-only
reconstruction probe produced five findings with valid span IDs in 600 output
tokens. It is excluded from the paired totals; the final decision arms had no
primary disagreements. Unit tests exercise reconstruction reuse across disputes.

## Full-batch reconstruction and checkpoint recovery

Paper-level blind reconstruction has its own 16,384-token limit. Binary/relation
packets retain the 4,096-token limit. A length-truncated reconstruction retries
once with twice its configured allowance; no incomplete reconstruction is accepted
or treated as evidence of ineligibility. Both attempts retain usage.

The mainnet decision-only replay runner supports explicit `--resume`. It archives
the prior paper workspace, restores its original usage journal exactly once, and
reuses only successful responses whose exact request, stage and recorded provider
ID match. Current schema and semantic validators run again. Reused calls produce
checkpoint markers without new usage events; prior failed attempts remain counted.
Requests interrupted without responses have unknown billing and are exposed in
recovery metadata. This is recovery within one experiment, not a zero-token cache
hit or reuse of historical compact judgments. Production does not install this
benchmark-only recovery hook.

Full-size packets can select a representative that another packet excludes. Draft
and audit proposals now request bounded repairs for those reference chains. The
model must select a supported retained representative, split the unit, or exclude
unsupported members; code never promotes an excluded anchor. Audit corrections
that require repair remain provisional and receive another global source audit.
At most three audit rounds are allowed, and final affirmative approval plus all
mechanical and scientific checks is still required before scoring.

The full-batch replay runner accepts up to 40 paper workers. When running multiple
batches concurrently, divide that allowance across the processes rather than
starting 40 workers in each. Concurrency and checkpoint recovery are recorded
separately from output-token savings. Provider-reported reasoning counters are
retained even when requests explicitly disable reasoning.
