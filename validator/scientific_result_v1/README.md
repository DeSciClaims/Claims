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
The older diagnostic interface remains available for miner preflight.

The default source reader and primary judge use Qwen3.8 Flash, the independent
secondary uses DeepSeek V4 Flash 0731, and arbitration uses Kimi K2.5. Each request
allows 32,000 source-evidence tokens and a separate 32,000-token structured packet,
with up to two additional source-context retrievals within the same allowance.
Judge output is capped at 16,384 tokens. These are validator settings, separate
from miner execution budgets. Routes, limits, prompts and implementation identities
are retained in the frozen evaluation configuration. The owner-approved testnet
recovery profile allows **two hours of validator judging per paper**; the legacy
default remains 30 minutes. This changes no miner cost, token or runtime allowance.
Historical clocks and attempts remain immutable when a revised policy starts a
new common judging generation.

The 32,000-token allowance applies to each request, not the whole paper. When
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
