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
   eligibility checks identify scientific prose before sampling. A committed
   random seed selects common content and table checks for every miner.
3. **Check the science.** DSPy programs compare each original output with the
   selected source evidence. They check complete findings, including attachments,
   context, grouping and provenance. Separate checks assess table transcription
   and the support for submitted scientific assertions.
4. **Review exceptions.** Independent judges review failures, unresolved findings
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
consistent current generation of scores.

## Which sources are in scope?

**Only sources actually supplied to miners.** A supplement or external table that
was not supplied is not a coverage requirement. Supplied supplements remain in
scope. A paper mentioning a missing supplement does not by itself block scoring.
Submitted scientific assertions still need support in the supplied evidence.

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
are retained in the frozen evaluation configuration.

Independent index batches use up to four request contexts per case, subject to a
global 16-request gate and four case workers. Packet splitting keeps scientific
dependencies together. Lossless transport pools repeated literal strings and
round-trips to the original retained assertion packet; it does not rewrite values.

## Retained evidence and replay

The content-addressed store retains supplied sources, Docling derivatives, original
JSON/CSV bytes, source tasks, sampling manifests, judge requests/responses,
mechanical findings, scientific verdicts and score components. Current-revision
checks prevent publication of stale score projections.

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
