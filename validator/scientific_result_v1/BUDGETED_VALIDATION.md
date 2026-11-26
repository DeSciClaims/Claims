# Optimized DSPy validation

This DSPy mode reduces repeated matching and evidence transport. The ten-paper
fixed-check comparison recorded $26.23 in scientific validation, plus $1.15 in
retained source preparation: approximately **$2.74 per paper across 20 pipelines**,
before infrastructure and unknown billing. The $1 target has not been reached.

The owner approved this mode for the experimental 25 November mainnet round.
Its frozen configuration must explicitly bind the optimized profile and advisory
scoring policy; strict production qualification is not asserted.

The original ten-paper testnet rerun recorded $103.91: $88.63 in output matching,
$9.90 in source retrieval and $4.57 in primary/secondary/arbitration judgments.
Recorded spending excludes unknown billing and infrastructure. Many original
checks remained unresolved, so lower cost alone does not establish improvement.

## Changes

1. Reuse the extraction's existing lexical/provenance index. Inspect a shortlist
   of original candidate objects instead of scanning every 32-object batch again
   for each source finding. Later scientific packets retain full original values,
   relationships, closures and context. A shortlist miss remains unresolved;
   it never establishes an omission by itself.
   Version 3 instead sends the **complete original extraction JSON** plus its
   computed closure views when they fit the model context. This is usually much
   smaller than repeated object/property wrappers. The index supplies navigation
   hints; it never restricts that complete scan. Several authored source findings
   share one complete JSON document per batch. Only a complete scan can certify
   absence. Large outputs fall back to the explicitly incomplete shortlist.
2. Batch ready matching requests for the same miner and role. Repeated original
   source anchors and candidate values are transmitted once per batch, using
   explicit references to exact shared values.
3. Reuse a local source index across miners and cases. Retrieve original captured
   paragraphs, immediate neighbours and explicit table/tile context in code.
   Mandatory witnesses and exact quotation hits are retained. Oversized mandatory
   evidence is unresolved rather than truncated. Partial retrieval cannot prove
   an assertion is unsupported throughout the paper.
4. Batch ready scientific checks for the same miner, operation and judge role.
   Every case keeps its own verdict, dimensions and original pointers. Secondary
   review remains blind; arbitration sees only the corresponding case's reviews.
   Complete independent shards within a root case can run in parallel too.
   Batch planning uses the actual model context minus its output reserve;
   every individual scientific case retains its existing source/packet bounds.

All paid requests still use the existing DSPy runtime, approved model routes,
provider-default routing and durable billing journal. Completed per-case results
retain their original batch response and run receipts; a completed case can resume
without another paid call. Technical failures remain failures under the owner’s
no-retry policy. A semantically invalid case does not discard its valid siblings.
The current version uses a nested typed response contract: every batch item
contains a normal JSON result object. This avoids malformed JSON inside
model-authored strings. The Qwen primary route uses JSON-object mode with the
complete canonical contract supplied once in model input, because Qwen rejected
the nested provider-constrained schemas. DeepSeek and Kimi retain typed provider
schemas, which passed their probes. Normal production requests keep
their existing strict provider schema. Code still enforces the full canonical
scientific contract independently for every experimental result.
The transport reader validates each inner scientific result independently, so
one invalid result does not discard valid siblings. Exact keyed maps
use the same lossless conversion as the existing DSPy adapter. Missing decisions,
duplicate keys and trailing text are rejected. A decoder correction can recover
a previously paid response without another provider call; its earlier receipt is
retained in an explicit recovery link.
Scientific source witnesses may name the original anchor ID or the exact shared
source alias explicitly supplied to that case. The decoder checks the alias's
content hash and expands it to the original ID, recording the mapping. It never
borrows another case's source, supplies missing response fields, or guesses an
unknown identifier. Saved paid responses can use this same audited decoder
without another request; previously unreached independent reviews still run.
Complete JSON matching may echo the provided component-manifest hash instead of
repeating its full ID list. The decoder expands only that exact, explicit hash
reference to the supplied manifest; missing or incorrect certifications fail.
The complete result contract includes all required fields and the exact scientific counterexample
code enum. Unsupported codes remain invalid; they are never guessed or renamed.
A frozen 250 ms coalescing window allows ready cases to share a batch despite
differences in local preparation time; it never waits for another case's verdict.

## Run

Use a separate output directory for each immutable experiment:

```bash
python scripts/evaluate_scientific_saved_paper.py \
  --bundle /path/to/saved/inputs.jsonl \
  --output /path/to/new/diagnostic \
  --env-file /path/to/model-credentials.env \
  --sampling-secret-file /path/to/original/sampling-secret.bin \
  --harness dspy --failure-tolerance --minimum-resolved-fraction 4/5 \
  --runtime-controls /path/to/no-retry-controls.json \
  --budgeted-validation
```

Only model API keys are read from the environment file. The diagnostic driver
does not contact the competition backend or publish weights. The profile is frozen
inside its execution policy. The diagnostic CLI rejects production mode and Hermes.
The competition factory separately permits the frozen profile only for explicitly
approved experimental advisory scoring.
The strict receipt remains HOLD with `BUDGETED_DIAGNOSTIC_ONLY`; conservative
diagnostic scores and unresolved checks are reported separately. Strict guide
replay is not asserted for these experimental batch/retrieval receipts.

Provider-free receipt replay recognizes this experimental HOLD marker. Identical
packet references retained during recovery are verified once without rewriting
the original receipt or adding scientific task credit. Conflicting references
still fail, and the experimental marker cannot appear on a strict FROZEN receipt.
Competition ranking additionally requires the round-bound advisory policy and
replays its separate conservative score receipt; recognizing the marker does
not qualify the experimental profile as strict guide validation.

## Compare

An offline pass can measure recovery of the old matcher's candidates on exactly
the saved source cards, without sending anything to a provider:

```bash
python scripts/benchmark_budgeted_retrieval.py \
  --saved-root /path/to/original/live \
  --output /path/to/new/offline-retrieval.json
```

The original evidence store is opened read-only. Earlier matching candidates are
a reference, not a truth corpus. Payload token counts exclude provider instructions
and response schemas; they are not bills. `--candidate-components` compares shortlist
sizes offline without activating a changed live profile.

For live runs, compare all twenty outputs per paper, confirmed and unknown costs,
gate outcomes, number of scientific tasks, weighted C/T/P completion, unresolved
causes and judgments on identical comparable checks. Fresh source-task generation
can change cards even with the same paper and sampling seed; report that change
rather than interpreting fewer checks as a saving. Scientific sampling, scoring
weights, miner output bytes and model identities are retained.

### Fixed-check comparison

Use the exact old scientific-check ledger to avoid source-authoring variation:

```bash
python scripts/compare_budgeted_saved_checks.py \
  --evaluation /path/to/original/paper/evaluation \
  --output /path/to/new/fixed-check-comparison \
  --env-file /path/to/model-credentials.env
```

This reuses the original mechanical gates, source frame, authored tasks, sampled
regions, assertion ownership and score denominators. It recomputes matching and
scientific judgments for exactly the original root checks. The source store is
opened read-only and missing source-to-table correspondences remain unresolved.
It reports incremental model cost, completion, and agreement on checks both runs
resolved. Historical preparation/gate cost is excluded from incremental billing;
include that recorded cost separately when estimating a complete cold evaluation.
No reused decision is presented as a new independent judgment or a free production
preparation step. The original failures and scientific assertions stay unchanged.

## Experimental mainnet activation, 25 November 2026

The reference baseline remains unchanged. All active stages use scientific Q
scoring and the existing code, team, advancement and tie rules; Jev is not used.
The published plan is 20 prescreen / 120 screen / 500 final papers, advancing
12 challengers (two per team), then three (one per team). Normal small-field
stage skipping applies. At least 10 / 60 / 250 common usable papers are required.
This 50% paper floor is separate from the advisory 80% C/T/P completion target.

Runtime limits: 10 source preparation workers, 10 scoring papers, four miner
outputs per paper and 20 root checks shared across those outputs per paper. A host-wide file-lock gate
limits preparation and scoring together to 500 in-flight model requests.
These are ceilings, not guaranteed utilization. Provider routing remains automatic,
with one provider attempt per request. Completed case receipts are reusable.

Miner execution limits remain $0.10, 2M tokens, one hour and 4GB per paper.
The planning budget is $1,200. Model identities and the round's burn window
remain unchanged. Rule amendments preserve intake, original history and baseline.
