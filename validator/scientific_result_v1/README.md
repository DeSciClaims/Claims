# How the structured-result validator works

The validator checks whether an extraction faithfully represents a scientific
finding: its numbers, classifications, relationships and conditions together.
A correctly copied standard error attached to the wrong estimate is still wrong.

Competition evaluation uses the supplied paper as its reference. Worked examples
help test the implementation; they are not gold extractions, and matching their
numbers of fields, results or families earns no credit.

## From a submitted package to a score

1. **Check the package.** Code validates the extended v1.1 schema, identities,
   references, graph closures and the actual separate CSV files.
2. **Select checks from the source.** The validator runs Docling on the supplied PDFs
   independently and freezes its text/OCR, captions and native tables. Source-only
   eligibility checks identify scientific prose and numerical-table blocks before sampling. Contents pages and navigation lists are retained as source evidence but excluded from the scientific table population. A committed
   random seed selects common content and table checks for every miner.
3. **Check the science.** DSPy programs compare each original output with the
   selected source evidence. They check complete findings, including attachments,
   context, grouping and provenance. Separate checks assess table transcription
   and the support for submitted scientific assertions.
4. **Review exceptions.** Source-review disagreements, including unresolved secondary
   interpretations, go to the source arbiter. Independent judges review failures, unresolved findings
   and a reproducible 10% sample of primary passes. An arbiter resolves disagreement.
5. **Calculate and retain the score.** Code calculates exact fractions and retains
   sources, original JSON/CSV bytes, samples, judgments and score components for replay.

| Component | What it measures |
|---|---|
| **G — Mechanical gate** | Does the package satisfy the mandatory mechanical requirements? |
| **C — Scientific content** | Is sampled scientific information represented correctly and completely? |
| **T — Table fidelity** | Are sampled numerical-table rows and their interpretation faithfully preserved? |
| **P — Output reliability** | Are the sampled groups of submitted scientific assertions supported? |

For a paper with scientific content and qualifying numerical tables:

**Q = G × P × (7C + 2T) / 9**

Without qualifying numerical tables, **Q = G × P × C**. A table-only paper uses
**Q = G × P × T**. A source with neither is ineligible for scoring.

Numbers, links, conditions and result families are assessed together in C and P;
there are no separate object-count or family-count bonuses. Table CSVs have their
own T checks. Optional data/code availability assertions use the extended schema's
V28–V30 rules and receive source-grounded review when applicable.

## Zero, pending review and corrections

**First-round policy:** an explicitly opted-in round uses an **80% evaluation
completion target as an advisory flag**, while every miner receives a conservative
numeric ranking score even below it. Unknown checks retain their denominator
weight and receive zero demonstrated credit. Scores, bounds, component completion
and failures are retained separately; evaluator errors are not confirmed miner
errors. See [First-round scoring](FIRST_ROUND_SCORING.md) for the formula, recorded
metadata and round-bound activation. The strict behavior below remains the default
for configurations without this first-round amendment.

A mechanical failure gives the miner **G = 0 and Q = 0**. A schema-valid
`review_required` closure with the required explanation does not automatically
fail the entire package; its scientific meaning still needs review.

A validator, provider or source-evidence problem produces a **common HOLD**:
Q remains null for every miner on that paper, and pending review emits no numeric
pair scores. An unresolved judgment is not a measured zero. A terminal common
exclusion uses explicitly flagged transport placeholders that ranking must skip.

If a source task or judgment is corrected, the original miner outputs stay
immutable. Every affected miner is reassessed under the common correction.
The earlier receipt remains in history; ranking and publication must use a
consistent current generation of scores. Independently confirmed source-inventory
defects can revise the common source frame at most twice, within the same judging
clock. No miner output changes.

After bounded recovery is exhausted, an immutable common exclusion records null Q
for the entire paper. The backend can atomically release the next paper in the
precommitted reserve order and schedule every entrant, subject to the frozen
replacement limit and available budget. The original paper remains in the audit
history. Old rounds retain their original full-sample minimum. New rounds can
freeze `claims_common_eligibility_v1`: the greater of the declared absolute stage
minimum and the planned sample multiplied by an exact fraction, rounded up. The
owner-approved starting fraction is **1/2**: a ten-paper testnet requires five
eligible papers. Mainnet retains absolute floors of 20/120/500/320 for
prescreen/screen/final/week-one. The fraction, absolute floors and planned counts
must be frozen before intake; none are applied retroactively. Replacements are
requested only after the scheduled grid finishes and falls below its minimum.
An unmet minimum produces a failed stage with diagnostic results and no weights.

## Which sources are in scope?

**Only sources actually supplied to miners.** A supplement or external table that
was not supplied is not a coverage requirement. Supplied supplements remain in
scope. A paper mentioning a missing supplement does not by itself block scoring.
Submitted scientific assertions still need support in the supplied evidence.

Qualitative findings are assessed at the level actually reported. An unreported
effect size or exact p-value is not a source failure. Independently reviewed source
corrections may remove unsupported draft claims with concrete source witnesses
and arbiter confirmation; the original draft remains retained.

The schema can represent figure-derived values. OCR recovers printed text and
numbers; interpreting marker positions or curves needs additional capabilities.
The current source-evidence policy is `docling_pdf_derivatives_v1`: judges rely
on the validator's independently captured Docling text/OCR and table structure.
No page-image review, model-authored bounding boxes or image crops are required.
Printed figure text/numbers captured by Docling remain usable; graph values that
Docling does not recover are not silently invented. Parser omissions and OCR
mistakes limit what this evaluation can verify.

## Components and interfaces

The source-task implementation is in [`source_tasks/`](source_tasks/). It provides
source preparation, deterministic sampling, original-byte package indexing,
scientific review, exact scoring and provider-free replay. The competition adapter
in [`competition/scientific_scoring.py`](../../competition/scientific_scoring.py)
converts frozen scientific receipts into paper and baseline/challenger score rows.
With the first-round policy it also converts separately verified conservative
score receipts, preserving the original incomplete scientific receipts.
The older diagnostic interface remains available for miner preflight.

The default source reader and primary judge use Qwen3.8 Flash, the independent
secondary uses DeepSeek V4 Flash 0731, and arbitration uses Kimi K2.5. Each request
allows 128,000 source-evidence tokens and a separate 64,000-token structured packet,
with up to two additional source-context retrievals within the same allowance.
Judge output is capped at 16,384 tokens. These are validator settings, separate
from miner execution budgets. Routes, limits, prompts and implementation identities
are retained in the frozen evaluation configuration. The owner-approved testnet
recovery profile allows **two hours of validator judging per paper**; the legacy
default remains 30 minutes. This changes no miner cost, token or runtime allowance.
Historical clocks and attempts remain immutable when a revised policy starts a
new common judging generation. The 24 November owner-approved 128k/64k profile
supersedes the earlier 32k/32k profile for new runs; historical settings can be
read for replay, but cannot be executed under different effective limits.

These allowances apply to each request, not the whole paper. The complete request
plus its reserved response must still fit each model route's declared context window. When
source evidence is too large, complete independent findings or table rows are
checked separately with their shared definitions and qualifiers. For a check
that needs to search a larger source scope, primary and secondary models each
inspect every source batch independently. Their original supporting, conflicting
and qualifying passages are joined for the final scientific judgment. The
validator retains and replays the complete inspection manifests; a failed or
incomplete search cannot establish that information is absent. Batches create no
extra scoring units: every required check must pass for its original task to pass.
If one inseparable assertion or its necessary joint evidence still cannot fit,
the validator retains HOLD rather than discarding information.

The validator controls the entire DSPy request. `Operations.prepare_request`
builds the instructions, source/assertion payload and strict response schema.
`StageProgram` executes `dspy.Predict`; `StrictAdapter` serializes compact system/user
messages and supplies the schema once through `response_format`. `dspy.LM` and
LiteLLM send that request to OpenRouter. The schema is not repeated in the user
message. Repeated large enum schemas share `$defs` without removing allowed IDs.

Source-lookup batching uses the same adapter rendering as dispatch and counts the
message text, provider schema and response reservation. It also checks the largest
valid lookup reply, including every inspection echo and every anchor relevant to
every query. Requests split by queries and then source anchors when needed. Both
independent roles still inspect every original source/query pair exactly once;
replay checks that coverage and the unchanged original source and assertions.
Reply headroom is a planning target, not permission to truncate a returned witness.
The source/packet allowances, 16k judge output, provider routing and miner caps are
unchanged by this implementation correction.

Judge requests use OpenRouter default provider routing with fallback enabled and
`require_parameters=true`. Provider `only`, `order` and `ignore` restrictions are
rejected. Endpoint snapshots document model capabilities; they do not restrict
which compatible provider OpenRouter may select. Selected model IDs, strict JSON
schemas and provider-reported serving identities remain retained for audit.

Independent index batches use up to four request contexts per case, subject to a
global 16-request gate and four case workers. Packet splitting keeps scientific
dependencies together. Lossless transport pools repeated literal strings and
round-trips to the original retained assertion packet; it does not rewrite values.
Source transport similarly factors repeated document identities while preserving
every anchor, coordinate and literal. Retrieval keeps all mandatory witnesses and
adds optional nearby context only when it fits. Independently reviewable scientific
findings are separated by source-only task construction before any candidate matching.
Strict judge responses key checks by dimension to prevent duplicate dimensions;
replay retains their unchanged canonical verdicts.

## Retained evidence and replay

The content-addressed store retains supplied sources, Docling derivatives, original
JSON/CSV bytes, source tasks, sampling manifests, judge requests/responses,
mechanical findings, scientific verdicts and score components. Current-revision
checks prevent publication of stale score projections.

Original PDFs are read from their locked paper-storage URL and checked against
the frozen SHA-256. Trusted source preparation caches those exact bytes locally
for Docling and worker staging; it does not upload the PDF through the evidence
API. A cold controller cache resolves the frozen source reference and fetches
the original again with the same host allowlist and hash check. Evidence
derivatives and scientific receipts remain in the shared content-addressed store.

Replay a retained receipt without contacting a model provider:

```sh
python -m validator.scientific_result_v1.source_tasks replay \
  --store /path/to/evidence-store \
  --receipt-sha256 RECEIPT_SHA256
```

The replay command checks retained artifacts and prints the receipt state and each
output's score. It does not issue new scientific judgments.

See [implementation and operation](IMPLEMENTATION.md) for entry points and
[the validation guide](../../schemas/scientific-result-extraction/v1.1/VALIDATION_GUIDE.md)
for the evaluation policy, sampling rules and response contracts.

## Latest verification

The latest retained check for each example paper completed on 21 November 2026.
Three receipts are FROZEN; Genther remains HOLD. All four passed provider-free replay. Shukla, Ohtsuki
and Perel use the four-paper r11 run; Genther uses the fresh focused r12 run
after making the complete required source-anchor list explicit. These receipts
are not one competition generation or a live ranking.

| Paper | G | C | T | P | Q | Verification |
| --- | --- | --- | --- | --- | --- | --- |
| Shukla | 1 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | FROZEN; r11 |
| Genther | 1 | — | — | — | — | HOLD; r12 focused |
| Ohtsuki | 0 | — | — | — | 0.0000 | FROZEN; r11 |
| Perel | 0 | — | — | — | 0.0000 | FROZEN; r11 |

Genther: EVIDENCE_LIMIT: No complete independent partition fits the 32000-token bound; RESPONSE_INVALID: Technical response failure after one identical-request retry

Genther's remaining oversized caption case passed a separate fresh-source targeted
check after the caption-scope clarification: one packet at 8,364 tokens. Its earlier full-run HOLD, including provider
429s and malformed matcher responses, has not been replaced with a new score.

Ohtsuki and Perel retain seven and eight V13 graph errors respectively; scientific
review is skipped and Q=0. No miner output was repaired.

All 148 regression tests pass. All 98 distinct dispatched scientific-review
packets decode exactly to their retained audit records; the largest is 30,959
tokens. Table matching preserves original CSV coordinates. Source-only consolidation
merges restatements before targets freeze, retains every original source witness,
and receives independent review against both drafts. Full scientific dependencies
remain in review packets.

The workspace report and per-run inputs, calls, snapshots and receipts are under
`outputs/claims-validator-packet-and-table-verification-20261121`. Latest selected
receipts recorded $5.591689 plus 3 unknown-billing attempts; earlier trials
have separate billing records. This is validator expense, separate from miner limits.

**Scientific judge accuracy is unmeasured.** Full conformance/pilot work and remaining
production integration are separate from this execution and replay check.

## Recovering a failed testnet evaluation

An authenticated administrator creates a linked recovery using the backend's
`POST /admin/competition/rounds/{round_id}/scientific-recoveries`. The immutable
manifest names the original sample, exact source/output-pool hashes, original
receipts, target configuration, reason and actor. It cannot change extraction
limits or activation mode. The original failed round stays intact and issues no
new weights. Public recovery progress appears separately on that round's page.

The runner is:

```sh
python -m validator.scientific_result_v1.source_tasks.recovery \
  --backend-url https://api.claims111.ai --round-id ROUND --run-id RECOVERY_ID \
  --root /protected/evidence --max-workers 2
```

`CLAIMS_COMPETITION_CONTROLLER_TOKEN` and the approved judge API keys come from
the existing protected service environment. They never enter the manifest.
Ordinary source preparation also honors the frozen OCR profile and performs one independent Docling parse, without a redundant preliminary parse. An optional frozen `request_budget` applies the same reservation guard to ordinary source and judging calls.

The runner reads sealed packages, keeps the original sampling secret, binds its
commitment to the revised policy, and never executes a miner. It reuses retained
independent Docling captures, or reparses the same supplied PDFs under the frozen
OCR profile. No supplement is fetched and no image crop is sent to a judge.

An unchanged-policy FROZEN result can be retained. A changed scientific policy
requires re-evaluation for every entrant on its affected common sample. Exact
request caches remain reusable only when prompts, routes, policy, schema and
payload match. Paper checkpoints and judging clocks use recovery-specific keys.
Source preparation and post-preparation judging have separate persistent clocks, matching the ordinary competition flow. Restarting the same recovery does not reset either deadline or spend. A new policy
requires another audited recovery manifest rather than overwriting a completed
result. Provider-free replay verifies a FROZEN result before it is checkpointed.

Each completed model attempt has a durable completion journal before its audit
heads and terminal status are published. If an evidence write or its HTTP reply
fails, resuming on the same evidence store finishes those writes from the saved
response, usage and receipt. It does not issue another model call or turn a
successful response into a model failure.

A durable diagnostic budget reserves the declared per-request envelope before
dispatch. Unobserved costs keep that reservation across process crashes. Actual
costs remain separate from reservations; unknown calls are never reported as free.
When in-flight reservations temporarily fill the allowance, queued calls wait
within the existing judging deadline. Only settled costs and unresolved charges
can exhaust it permanently; temporary concurrency pressure is not a scientific
failure.
Captured OpenRouter generation IDs can be reconciled against [generation
metadata](https://openrouter.ai/docs/api/api-reference/generations/get-generation),
retaining both the old usage record and the provider proof. A request that never
reached provider dispatch records known zero. Miner limits remain $0.10, 2M tokens
and one hour. Diagnostic recovery results do not crown or publish weights.


For a pinned testnet runtime with unreliable backend connections, operators can
run `python -m competition.scientific_evidence_relay --port 18765 --upstream
https://api.claims111.ai --round-id ROUND --run-id RECOVERY` and point that
recovery's `--backend-url` at `http://127.0.0.1:18765`. The relay binds loopback,
requires the existing controller credential, and permits only the declared
recovery's evidence/checkpoints and immutable model audit objects. It retries
transport faults with the exact original body, never conflict/auth failures,
worker claims, administrative changes or awards. Confirmed immutable blob uploads
are reused after hash/receipt checks. This changes transport only: the original
runtime, policy, deadlines, requests and saved judgments remain unchanged.


When a delivered CSV's data-row count disagrees with Docling, two independent
judges first check the source layout, with an arbiter for disagreements. They see
only the captured source rows and headers, never the candidate CSV/count. A verified
source count can support a mechanical failure; uncertain source boundaries hold
or exclude the paper for everyone. CSV/declaration errors still fail directly.
Retained header rows are mapped as context without rewriting the CSV or its row
coordinates. Review coverage and witnesses are checked again during replay.


Bounded evidence requests search beyond adjacent text already present in a tile.
Table-note and adjacent-text requests can retrieve original parsed text and OCR
from the same captured page, including notes that Docling did not link to a table.
Every added anchor remains complete, hash-bound source evidence within the existing
128k source allowance. The reader never invents missing text or fetches outside sources.
Diagnostic recovery supports ten concurrent paper reviews while limiting the
memory-heavy source-preparation phase to two slots; the shared per-process model
request gate remains sixteen. This changes scheduling, not sample size or credit.

The v6 judge transport also encodes consecutive source anchor IDs as exact,
readable ranges. It preserves every ID, its order, zero padding and source scope;
replay expands the ranges back to the identical immutable packet. This avoids
spending most of a 64k assertion allowance on repeated administrative identifiers.
The canonical source, scientific assertions, qualifications and scoring bits do
not change. Source-card generation also exposes its existing outcome/card
consistency requirements in the provider schema, including corrected proposals.

### Selectable judge harness

The default harness is `dspy`. A new policy can freeze `hermes-files` through
`freeze_settings(..., harness="hermes-files", hermes_python=...)`. Runtime creation
checks the selected model roles, tool contract, provider parameters and the actual
Hermes source/dependency identity. Existing policies never switch automatically.
Set `CLAIMS_SCIENTIFIC_HERMES_PYTHON` to the frozen interpreter when running a file
policy; no shell, web, general file-write, memory or delegation tools are exposed.

Both modes use the same sampling, mechanical checks, independent reviews, arbiter,
C/T/P/Q calculation and correction/replay pipeline. File mode gives each operation
fresh task/instruction/schema files and lossless source/candidate views. Matching
can inspect the complete candidate corpus in one workspace, using a paginated
combined component map. Full-read proofs count the exact original component spans
in that map as well as individual files. Source-only target
construction includes original neighboring Docling page text/OCR as context.
Scientific judges receive the complete captured source corpus for supplied PDFs,
including notes outside the sampled row block. Owned source regions and samples
stay unchanged; other source text supplies witnesses, never additional targets. Omission credit still needs a
complete candidate search. Search hits alone never establish absence.

File mode uses 80 turns per operation, 32,768 output tokens per model call and the
frozen two-hour paper clock as its deadline. Actual decoded model
requests are checked against the frozen model context capacity. Large files do
not consume context until read; no source text or scientific assertion is cut to
fit a DSPy prompt-size bound. File-read coverage, original byte hashes, schema and
scientific-contract checks remain mandatory. Hermes context compaction uses the same
frozen route capacity and output-token reservation as the native request guard.
Assertion audits use mandatory ownership groups; all groups must pass for the
original bucket bit. Complete finding dependencies remain together. The submission tool exposes the response structure as native function arguments.
Large identity lists remain in the schema file and are checked without repair.
Validated artifact submission stops the conversation immediately. If a working turn
ends without an artifact, one explicit completion-feedback turn can continue the
same history and shared iteration budget. Plain prose is never a judgment.
Profile 9 ends an attempt after three identical validation failures. The existing
bounded controller retry starts a fresh context and retains the failed attempt;
completed operations, the paper clock and scientific decisions are preserved.

The submission tool returns concise errors with field paths and missing/unexpected
IDs rather than echoing a long invalid array. Source-card ownership and preservation
checks also run inside the agent's submission loop, using the same controller
validators. The agent can correct a draft before the operation ends; no scientific
value or citation is automatically repaired. In harness profile 8, fixed inspection
ID lists are supplied by the controller after complete retained-file reads have
been verified. The model supplies all scientific matches and witnesses. A retained
raw submission and metadata receipt let replay reproduce this envelope; a supplied
incorrect ID list is rejected rather than overwritten.

Every native provider call, including auxiliary calls, must use that role's model
and automatic provider routing. Native costs and tokens are retained separately
from Hermes estimates. Unknown billing stays unknown. Workspaces and conversations
are chunked into content-addressed evidence so replay does not depend on a local
VM directory. Source eligibility, final judgments and routing are checked again
without provider calls.

For an existing Hermes installation, create an isolated interpreter using its
installed dependencies and the validator's tokenizer, without changing that
installation or downloading packages:

```sh
python scripts/setup_scientific_file_harness.py \
  --hermes-python /path/to/hermes/venv/bin/python --output /path/to/new/runtime
```

Evaluate a trusted validator-owned saved-input bundle end to end:

```sh
python scripts/evaluate_scientific_saved_paper.py \
  --bundle /path/to/trusted-paper-inputs.jsonl --output /path/to/new/evaluation \
  --harness hermes-files --hermes-python /path/to/new/runtime/bin/python
```

Use `--model SOURCE_READER=minimax/minimax-m3.1-flash` to freeze an explicit
approved model override. It requires that model's route snapshot in the trusted
bundle. Judge independence is still enforced. Resuming with different model roles
requires a new evaluation directory.

Supply model credentials through the environment or `--env-file`. The driver only
loads model keys, uses a fresh committed diagnostic seed, preserves the extraction
JSON/CSVs, and performs fresh source eligibility and scientific review over the
retained independently captured PDF/Docling evidence. It creates no backend ranking
rows or weights. Reusing the output directory resumes only matching frozen inputs;
a changed harness or bundle requires a new evaluation directory.

`scripts/benchmark_scientific_file_judges.py` remains available for individual
retained requests (`--cases`, `--output`, `--hermes-python`). Valid JSON, successful
file reads or a single paper do not establish scientific accuracy or production
qualification. Framework comparisons must also account for changed prompts,
context availability and output caps.

Transport v7 stores repeated assertion-wrapper keys in column templates. All
original properties, explicit absence markers, relations and closure views stay
unchanged; decoding reconstructs the exact rich packet. Scientific quantities
are never summarized or split apart to make the packet fit. The source lookup
checks audit ownership and its material dependencies rather than independently
verifying every unrelated partial co-member header.

A diagnostic may freeze `source_parser.retained_capture_frames` and an optional
`retained_source_run_id` to reuse earlier independent Docling captures. Each
capture must match the original source-packet hash, its origin configuration,
parser implementation, installed Docling version and requested OCR scale. The
full original frame proof is replayed before reuse. Incompatible parser settings
fall back to parsing the original supplied PDF; mismatched source/policy bindings
are rejected. Only immutable bytes are shared from a read-only cache. New source
eligibility decisions, tasks, scientific judgments and clocks remain independent.

### Local proprietary-model comparisons

`scripts/compare_scientific_proprietary_harnesses.py` replays retained failed
source reviews through DSPy and Hermes using the same scientific inputs, model,
reasoning setting and output allowance. This diagnostic is separate from the
competition model allowlist. Proprietary routes require an explicit
`local_proprietary_comparison_v1` execution policy containing an explicit v1,
v2 or v3 diagnostic model profile; they cannot be activated by changing a
production model name alone.

OpenAI-compatible DSPy transport nests the original response under `result`,
converts disjoint tagged unions into supported schema constructs, and validates
the unwrapped response against the complete original contract. Array ordering and
all scientific rules remain mandatory after generation. Unsupported sampling
parameters are explicitly omitted and recorded in the diagnostic profile.

A shared SQLite budget reserves the worst-case call cost before dispatch across
both processes, settles provider-confirmed charges, and conservatively retains
unknown charges. Route/model/price snapshots and actual requests are retained.
The full-paper driver also accepts `--diagnostic-profiles` and
`--sampling-secret-file` to compare the same supplied source and sampling secret.
Generated source tasks can still differ, so full-paper Q scores alone do not
measure harness accuracy. These diagnostics publish no competition scores or
weights.

The local comparison now defaults to **no spending cap**. Cost estimates,
provider-confirmed charges and unknown charges remain in the same ledger. An
explicit `--cap-usd` can enable a cap for a separately requested experiment;
`--no-spend-cap` remains an explicit uncapped option. Removing a cap from an
existing ledger preserves its history and records the change.

Uncapped v2 profiles use one shared model-request slot across both harnesses.
The specific OpenRouter 402 carrying `in_flight_budget_exhausted` and
`openrouter_in_flight_budget` honors `Retry-After` and retries the same transport
request inside the existing paper deadline. This does not consume a scientific
review retry. Other billing errors remain errors. Hermes's idle watchdog uses the
remaining operation timeout so intentional queueing/cooldown is not misclassified
as a hung provider.

OpenAI provider-schema compilation removes redundant common parents from complete
outcome branches and prunes unreachable definitions. Large identity enumerations
that exceed OpenAI's grammar limits are checked against the original full schema
after generation. Every original field, reference restriction and scientific
validation rule remains mandatory locally; the compiler does not repair model
decisions or evidence.

DSPy-only v2 diagnostics can explicitly freeze `max_output_tokens=65536` instead
of 32768 through `proprietary_diagnostic.policy`. Freezing checks the saved strict
endpoint output capacities before accepting the larger allowance. This does not
change competition defaults or existing run settings. Run this diagnostic alone
to avoid sharing its request queue with a Hermes comparison. DSPy's internal
truncation warning is bypassed because it assumes a temperature parameter exists;
the recorded runtime saves the actual response, usage and finish reason first,
then rejects truncated output with `TruncatedStageError` and retains its billing.
The error distinguishes the requested output allowance from reported actual token
usage; a provider `length` response does not prove that the requested cap was used.
The transport also removes a string regex when every member of a retained exact
enum satisfies it. This is a logically redundant constraint, and the full original
schema still validates the answer. Deferred identity enums retain their regex.

For DSPy-only parallel diagnostics, freeze a v3 model profile with
`policy(model, context_window_tokens=..., budget_database=...,
unlimited=True, max_output_tokens=65536, max_in_flight=20)`. All four roles must
share the same ledger and ceiling. Native API requests share up to twenty
cross-process slots, and the Operations gate and outer scientific/source-task
workers use the same ceiling. Nested searches keep smaller worker pools to avoid
multiplying waiting threads; their requests still use the shared twenty slots.
Scoped runtimes retain independent usage ledgers. Independent source batches can
run concurrently, while a task's dependent review steps remain ordered.

Exact transient OpenRouter 402 admission failures and HTTP 429 rate limits honor
Retry-After with jitter. A backing-off request releases its slot. Generic billing
failures are not retried. Existing v2 single-request profiles, completed evidence
and production concurrency remain unchanged; stopped evaluations are not resumed
by preparing a new profile.

Local diagnostic routes also support `openai/gpt-6-luna`,
`openai/gpt-5.6-luna`, `openai/gpt-5.4-mini` and `openai/gpt-6.1-terra`.
V3 profiles can explicitly select low or medium reasoning. Availability, endpoint
capabilities and prices must be snapshotted before a paid run. These candidates
have not yet been qualified as scientific judges; keep three distinct judge model
identities when benchmarking a cheaper source reader/primary and secondary.

Generated matching/judge schemas enumerate the real descendants of fully shown
original JSON literals as valid witnesses, matching the existing scientific
verifier. A nested column or annotation is available only if its complete parent
literal was rendered; unseen siblings and invented array indices remain invalid.
Schema retry feedback reports bounded field paths and constraints instead of
dumping the whole response before the actual error. Scientific judgments and
references are never automatically repaired.
Source-card drafts, requirements, and corrected proposals constrain citation
strings to the actual supplied anchor IDs during generation as well as after it.
Evidence requests may still seek additional captured context; citations cannot
substitute a document/item ID for an available anchor ID.
