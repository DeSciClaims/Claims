# Scientific-result validator: implementation and operation

For the high-level design, start with the [overview](README.md).

There are two entry points. `source_tasks/` implements source-based competition
scoring. The root diagnostic CLI provides the older per-rule preflight report.
Both preserve submitted extraction bytes and use controller-owned source evidence.
Neither accepts miner-generated validation reports as authority.

## Source-task competition scoring

The protocol is `claims-source-task-v1`, bound to the extended schema SHA-256
`78f5b279dfbf4f2fec5fc83b2219350e43e3fece5c58e94f81fb5d146d10a5e2`.
The authoritative policy is the amended [validation guide](../../schemas/scientific-result-extraction/v1.1/VALIDATION_GUIDE.md).
It includes the owner's supplied-sources-only policy and waived truth-corpus
requirement. Judge accuracy remains unmeasured.

`source_tasks.configuration.freeze_settings` constructs hash-bound settings from
retained approved route snapshots and the deployment execution policy. The chosen
routes are Qwen3.8 Flash for source reading and primary verification, DeepSeek V4 Flash 0731
for independent secondary verification, and Kimi K2.5 for arbitration. These judge
routes do not change the miners' allowed models or their $0.10 inference cap.
The per-call output allowance is 16,384 tokens. Credentials stay in host
variables. The current PDF evidence policy is `docling_pdf_derivatives_v1`, chosen
by the owner on 21 November 2026. The controller runs Docling on the supplied PDF
bytes; caches must be controller-owned and bound to the same SHA-256 and retained
parser metadata. No page-image review or LLM-authored crop geometry is required.
Native Docling text/OCR, table cells, spans, headers and section rows are retained.
Judges assess against those derivatives; their omissions and OCR errors remain a
limitation. The text-only DeepSeek secondary route can therefore review PDFs.

`source_tasks.docling_frame` derives PDF source frames directly from retained
Docling bytes. Its parser layout proofs can be rebuilt without a model call.
Source-only prose eligibility has independent classification and arbitration when
needed. Scientific content/archival/assertion judgments retain their existing
independent routes. Mechanical failures skip scientific judgment after source
eligibility is established. Additional archival context rows are mapped to their
original CSV coordinates without changing delivered bytes. Additional table exports
are reviewed as submitted assertions; they do not fail merely because Docling did
not detect a corresponding native table. Every qualifying native source table still
requires a corresponding delivered archive.

Run retained real-paper packages with `scripts/verify_scientific_example_papers.py`.
Required arguments are `--packages`, `--pdfs`, `--trusted-sources`, `--parsed`,
`--snapshots`, and `--output-dir`; `--reparse-pdfs` uses a validator-owned Docling
cache under that output directory. Missing caches are created from retained actual
PDF bytes. `--check-inputs-only` verifies original source hashes and package gates
without provider calls. Scope preserves the exact supplied source bundle, including
any supplied supplement, and fetches no missing supplement. Each case retains
inputs, frozen routes, Docling artifacts, original JSON/CSVs, calls and a JSON report.
These historical extractions predate the validator's seed commitment, so this is a
retrospective diagnostic rather than a live competition execution. Worked examples
are not gold inputs to the scorer.

The standalone harness supplies an explicit empty transport control because the
competition comparison contract requires a pair. That control is marked as a
diagnostic package failure, consumes no scientific judge calls, and is not a miner
submission or gold extraction. The report selects the historical extraction's
source-grounded Q and publishes no competition ranking.

Source-card construction accepts a judge's `NEED_EVIDENCE` request and retrieves
only captured anchors from the same immutable source frame, with at most two
context retrievals (each request allows 32,000 source tokens). Independent review can request
the same expansion. Added context cannot become an independent target outside the
original tile. Unresolved common task construction holds the score before further
matching/scientific calls. Oversized joint assertion packets still require complete
legal partitions; they must never be truncated or scored as missing miner content.

`table_matching.resolve_table` compares the complete original table declarations,
CSV headers/row labels and trusted source bindings. Primary and secondary calls are
blind; differing or ambiguous matches go to the arbiter. Scientific-content matching
still scans the full original assertion index. Providers return only typed requirement
matches; code derives candidate_refs from their union, so no unrelated second list
can inflate packets or cause an inconsistent response. DeepSeek routes are pinned
to advertised DeepInfra strict-schema endpoints; original and filtered snapshots
are retained and no fallback to another provider is implicit. The status-dependent
counterexample constraints are encoded in the provider schema, not just checked
after generation. Unassigned context cannot inflate every review packet.

`transport.judge_packet` losslessly factors repetitive property wrappers and original
property pointers. Exact literal strings, missing-property flags, original references,
relationships, closures, sidecars and CSV cells remain recoverable by `audit_packet`.
Version 4 also inherits repeated output aliases and derives identity, edge and
closure view headers only when their values exactly match retained original
properties. Repeated administrative IDs inside JSON literals use a collision-safe
identifier pool; decoding restores the exact literal strings. A differently ordered
closure array stays explicit. These changes reduce packet size without removing
scientific assertions or changing the 32,000-token bound. Older v1–v3 records remain
decodable, and a changed transport requires a newly frozen runtime configuration.
`judge_evidence` factors only repeated document SHA-256 values; document/anchor IDs
and source text stay unchanged. The 32k budget measures actual judge transport.
Rich immutable audit records remain available, and score replay checks transport
against the original records. Closure expansion uses the initially selected owners;
shared context does not recursively pull in every other consumer's result fields.

SourceTaskDraft.scope_kind is set from source only and independently reviewed.
SCIENTIFIC_RESULT retains joint result dependencies. SOURCE_CONTEXT covers given
protocol conditions/inputs and shared conventions; complete independent context
requirements can be mandatory shards with common task/source identity. Every
representation of each requirement remains in its shard. Explicit partial
context_headers preserve original identity/scope needed for contextual associations,
without pretending to supply unseen numeric fields. Numeric findings remain full.

P shards group record assertions and incident associations by scientific owner.
The child ownership list is packet.match.candidate_refs, not a duplicated parent
ledger in instruction text. Replay checks its disjoint exhaustive union against
the sampled parent bucket. Shared context does not merge every consumer into one
indivisible group. These checks preserve audit coverage while reducing duplicate
reviews of the same result context.

`response_schema.response_schema` binds the provider schema to the immutable
request: task/packet identities, applicable dimension names/count, available source
witnesses and provided counterexample pointers. It expresses existing outcome and
witness constraints with grammar-supported branches. Canonical response validation
still enforces exact distinct dimension/inspection sets, original-pointer resolution,
scientific witness rules and all scoring conditions; no scientific decision is
filled in or repaired by this binding. The provider still returns JudgeResponse.

The trusted flow is `prepare_paper` → sealed `SubmittedPackage` pool →
`PaperEvaluator.evaluate` → `adapt_scientific_receipt`. Main paper and supplied
companions are immutable `SourcePacket` documents. Preparation captures Docling
text/OCR and native table layout directly from the actual PDF bytes. The 32-byte paper seed is committed
before worker execution; parsing and inventory time are separate from the retained
1,800-second post-seal scientific evaluation deadline.

C samples six source tiles (initially three prose and three table blocks, with
shortage transfers); T independently samples four five-row table blocks. C averages
cards within tiles and blocks within tables, then gives equal weight to the two
applicable content strata. P samples up to four occupied source buckets plus a
mandatory UNANCHORED bucket. P starts from the committed ownership references;
it does not ask a model to rediscover those assertions. Source correspondence
search remains required for C/T. Complete independent shards share one original task
identity and combine by minimum, so splitting does not create scoring mass.

The package reader retains original numeric literals, null/missing distinctions
and RFC 6901 pointers. Matching uses exact source hits, indexed descriptor ranks
and complete fallback before certifying absence. Scientific checks receive the
original properties and graph/context; available CSV cells do not instantiate
missing scientific fields. No claim-unit count, canonical family organization or
semantic-copy veto governs this protocol.

Scientific judgments use strict response contracts and fixed parameters: 16,384
output tokens, temperature 0, top-p 1, one response, no tools and a 120-second
request timeout. Primary/secondary/arbiter identities are distinct. Technical
failures have one retained retry. All failures/unresolved cases and a deterministic
10% pass audit receive independent review; incompatible interpretations go to the
arbiter. Source evidence allows 32k tokens from the first call, with two context retrievals; complete
structured shards are bounded to 32k tokens. No truncation receives omission credit.

Evidence is content-addressed in `Store`, or in the private backend via `RemoteStore`.
Paper-head advancement is compare-and-set. Corrections archive/invalidate existing
score projections; rebuilding the complete paper score/pair grid is transactional.
Rank/publication checks reject stale receipts and incompatible comparison plans.
Backend SQL changes are included in the scientific-evidence migration and have not
been deployed by this work.

Replay a retained receipt without a provider call:

```sh
python -m validator.scientific_result_v1.source_tasks replay \
  --store path/to/evidence-store --receipt-sha256 RECEIPT_SHA256
```

Run the offline implementation checks from the Claims repository:

```sh
python -m pytest tests/source_tasks
```

The synthetic live smoke runner is `scripts/smoke_scientific_source_validator.py`;
`--help` documents its source fixture, route snapshots and output directory.
`scripts/preflight_scientific_routes.py` checks strict format, fixed parameters and
image delivery. Neither is an accuracy qualification or a live-round activation.

Automatic common replacement, sealed-reference fallback, exhaustive E01–E50
conformance and the full operational pilot remain integration/verification work.
Do not enable production based only on the smoke tests.

## Diagnostic validator CLI

The remaining sections describe the older diagnostic/preflight interface. Its
`submission_ready` decision is not the source-task competition score, and its
confidence-based reviews do not replace the categorical multi-judge source tasks.

The registry exposes all V01–V27 rules from the schema guide. It also exposes
V28–V30, the optional data/code-availability extension in the canonical repository
schema. Each rule has `pass`, `fail`, `review_required`, `not_checked` or
`not_applicable` status. Source reviews are DSPy judgments with evidence, not
mathematical proofs or a guarantee of complete source interpretation.

## Run locally

Install Claims with the `scientific-validation` extra for validation. Preparing
PDF sources additionally needs the `scientific-results` extra and provisioned
Docling model artifacts; preparation is separate from validation.

```sh
python -m validator.scientific_result_v1 \
  --extraction package/extraction.json \
  --report-dir reports/preflight
```

This performs deterministic checks. It exits nonzero because a structurally
valid package without scientific review is not submission-ready. It writes
`validation_report.json`, including every rule and the unresolved requirements.
The installed command `claims-validate-scientific` exposes the same interface.

Prepare trusted sources upstream, outside the miner's control:

```sh
python -m validator.scientific_result_v1.prepare \
  --paper-id P001 --pdf paper.pdf --supplement supplementary.pdf \
  --output-dir trusted/P001
```

`--prepared-dir` reuses a controller-owned Docling cache only after checking its
PDF hashes. `--required-sources missing.json` supplies an explicit list such as:

```json
[{"source_id":"S003","available":false,"description":"Required external numerical table"}]
```

Do not invent unavailable-source IDs as consulted documents in extraction JSON.
The manifest records unavailable requirements separately. The preparer never
fetches supplementary, dataset or code links. Its manifest hashes the actual PDF,
text and OCR files. The coordinator additionally pins the manifest hash in frozen
policy. A source-file change invalidates the bundle. Source IDs are rebound to the
submission's independently allocated extraction namespace using fixed source keys.

Run the complete trusted gate using a pinned paper ID and main-PDF hash:

```sh
OPENROUTER_API_KEY=... python -m validator.scientific_result_v1 \
  --extraction package/extraction.json \
  --source-bundle trusted/P001/manifest.json \
  --paper-id P001 --pdf-sha256 <controller-pinned-sha256> \
  --scope trusted --review --report-dir reports/P001
```

Credentials come from environment variables and are not saved in request traces.
`CLAIMS_INFERENCE_BASE_URL` and `CLAIMS_INFERENCE_TOKEN` support the scoped gateway.
A text-only reviewer cannot certify digitized coordinates: without trusted page
images these fields have a blocking review requirement. The preparer supports
`--images`, which hashes controller-rendered pages; reviewing them requires a
permitted vision-capable model. A caption or image placeholder does not establish
plotted values. No image capability is silently assumed for DeepSeek V4 Flash.

## Validation stages

1. Validate JSON structure and controlled vocabularies from the canonical schema.
2. Check unique definitions, typed references, parent paths and family ownership.
3. Recompute graph membership and boundaries without rewriting the document;
   check terminal components, compound cores and unresolved incident links.
4. Open actual UTF-8 CSVs, verify LF separators, final separator, quoting, headers,
   row counts, merged-cell spans and referenced coordinates. Reject symlinks and
   escaped paths. Check literal numeric-cell agreement, retaining source-review
   requirements where component/sign/unit interpretation is needed.
5. Review scientific attachments, value/equation fidelity, family boundaries,
   source/table coverage and optional availability metadata using DSPy programs.
   Review jobs cover assigned objects explicitly; invented IDs, missing verdicts
   and nonexistent evidence blocks are rejected. Low-confidence judgments require
   review. Every evidence citation resolves to original trusted source text.
6. Report the independent structural, package, scientific and completion results.

`submission_ready` requires resolved rules, complete table coverage, closed local
closures and a completed review execution. `miner_diagnostic` reports never grant
submission readiness or authoritative acceptance. `reference_preflight` can pass
before reference packaging, but the trusted coordinator still repeats validation.
A missing or failed reviewer remains unchecked, not passed. The `supplied_sources_only` evaluation policy excludes unsupplied supplements
and external tables from coverage requirements. Manifest notices about unavailable
materials are informational, even if the legacy key is `required_sources`.
Omission of a supplied supplement still blocks complete coverage.
Unaccounted fields require scientific review because the guide permits clearly
contextual content; the validator does not infer dependence from storage.

## Review budgets and provenance

The defaults bound reviews to 32 calls, 600 seconds, 16 million input characters
and a post-call reported-cost stop of $1 per package. Each call has an absolute
transport deadline clamped to the remaining review time, no hidden transport
retries, and bounded semantic retries. Budget exhaustion leaves requirements
unchecked and is a validator execution fault. Use a separately scoped gateway
review key for a hard financial cap: local cost checks cannot undo a provider's
charge, and timeouts can have unknown billing.

Reports bind package files, schema, source bundle and validator implementation by
SHA-256. Frozen scientific worker policy must pin both validator and schema digests;
a source/code change during validation is rejected. Use a separate reviewer runtime
for each concurrent invocation. Each invocation has a fresh trace directory, original source blocks,
model-facing requests, provider responses, verdicts and usage ledger. A previous
report is never treated as cached acceptance. Submission/source mutations during
review are rejected. Request traces are private controller artifacts, not miner
inputs or public worked examples.

## Integration and trust boundary

The miner's compatibility facade delegates mechanical checks here; its explicit
repairs and graph construction stay under `miner/scientific_result_v1`. The miner
writes a diagnostic report rather than declaring scientific completion.

`claims-reference-miner` performs the full shared preflight before issuing a
Bronze manifest and includes its report hash. Its scientific competition builder
ships the same validator and schema with a sealed scientific extraction profile.
The worker returns a `claims_scientific_package_v1` envelope containing extraction
JSON and the exact declared CSV texts. It cannot attach a trusted report.

The guest checks package structure; `competition.scientific_validation` recomputes
validation on trusted compute. `worker_controller` rejects non-ready outputs and
classifies reviewer execution errors as infrastructure faults rather than miner
faults. Reviewer billing is recorded separately from miner inference billing.
Existing `agent_v1` policy remains the default. Opt-in requires frozen policy:

```json
{
  "output_schema":"scientific_result_v1.1",
  "scientific_validation":{
    "model":"deepseek/deepseek-v4-flash-0731",
    "validator_sha256":"<validator-implementation-digest>",
    "schema_sha256":"<canonical-schema-file-sha256>",
    "bundle_manifest_sha256_by_paper_id":{"P001":"<manifest-file-sha256>"},
    "max_review_calls":32,
    "review_deadline_seconds":600,
    "max_review_cost_usd":1.0
  }
}
```

These fields belong inside `worker_policy`. Existing model whitelist, gateway and
budget fields are still required. Trusted-host configuration additionally needs
`CLAIMS_SCIENTIFIC_VALIDATOR_SOURCE_ROOT`, `CLAIMS_SCIENTIFIC_VALIDATOR_REPORT_ROOT`,
`CLAIMS_SCIENTIFIC_VALIDATOR_API_BASE`, and `CLAIMS_SCIENTIFIC_VALIDATOR_API_KEY`.
The validator digest is `digest(implementation_hashes())` from the shared engine/sources
modules; the schema digest hashes `schema_path().read_bytes()`. A source manifest is stored at `<source-root>/<sha256(paper_id)>/manifest.json` and
must match the frozen manifest hash. Source/reviewer credentials never enter the
guest. This is code integration, not a deployment or a migration of legacy scoring
rules; activate a new round format only with compatible downstream consumers.

## Verification status

The packet-construction and table-matching checks completed on 21 November 2026.
These are the latest verified receipts per paper: Shukla/Ohtsuki/Perel from the
four-paper r11 run, and Genther from a fresh focused r12 run after correcting the
source-anchor request. They are not one frozen competition generation or a ranking.
Three receipts are FROZEN; Genther remains HOLD. All four passed provider-free replay.

| Paper | G | C | T | P | Q | Verification |
| --- | --- | --- | --- | --- | --- | --- |
| Shukla | 1 | 1.0000 | 1.0000 | 1.0000 | 1.0000 | FROZEN; r11 |
| Genther | 1 | — | — | — | — | HOLD; r12 focused |
| Ohtsuki | 0 | — | — | — | 0.0000 | FROZEN; r11 |
| Perel | 0 | — | — | — | 0.0000 | FROZEN; r11 |

A dash means skipped or unresolved; a HOLD is not zero.
Genther: EVIDENCE_LIMIT: No complete independent partition fits the 32000-token bound; RESPONSE_INVALID: Technical response failure after one identical-request retry

Ohtsuki and Perel retain seven and eight V13 result-graph
errors respectively, so G=0 and Q=0. Their submitted packages were not repaired.
The latest selected receipts recorded **$5.591689** in validator charges, plus
**3 attempts with unknown billing**. Earlier failed trials and targeted
diagnostics retain their own billing records; this figure is not total development
cost and is separate from the unchanged $0.10 miner cap.

The historical r12 HOLD includes a caption/reporting-convention card incorrectly
classified as a scientific result, which activated most of a table. After a general
caption-scope clarification, fresh source drafting, consolidation and independent
review classified it as context. The targeted real-case check produced
one complete packet at 8,364 tokens, with
exact round-trip checks. The full Genther package was not rescored after that
prompt clarification; its earlier HOLD remains authoritative. Provider HTTP 429s
and malformed matcher responses also exhausted technical retries in r12.

The fixes are general:

- Caption/format references do not request all results in their table; findings
  explicitly stated in captions and numerical table row targets remain scientific.
- Table identity is matched against the original TableObjects and CSV headers,
  with blind independent review and arbitration. Table-fidelity checks retain
  actual CSV row coordinates, including section-label rows.
- Shared context does not recursively activate every consumer's result closure.
  Focal scientific objects and necessary dependencies remain complete. Unrelated
  family members use explicit original scope headers and cannot supply unseen values.
- Source tasks identify analysis and adjustment conditions. A source-only pass
  merges restatements of the same quantity before targets freeze. Both drafts are
  independently reviewed for scientific equivalence; every original cited source
  anchor must remain. The controller supplies the exact anchor list and identifies
  missing IDs in retry feedback. Distinct analyses and new findings stay distinct.
- Oversized packets may split by complete source requirements, repeating complete
  shared scientific objects where necessary. A single requirement cannot be split
  into incomplete candidate views. Independent source-witnessed identity reviews
  can exclude a different referent only when both reviewers agree; uncertainty stays.
- Lossless transport factors repeated exact literals, property names and source
  hashes. It preserves original values, JSON pointers, missing/null distinctions,
  relationships, closures and CSV text. Request-bound provider schemas and complete
  keyed match maps prevent binding and duplicate/missing requirement errors.
- Independent match batches run under the existing 16 global requests, four case
  workers and four contexts per case, with deterministic complete aggregation.

All **148 regression tests pass**, as do targeted Ruff checks and diff checks.
Retained-request checks decoded **98 distinct dispatched review packets**
byte-for-byte to their full audit records. The largest packet was **30,959 tokens**, within 32,000. Source evidence has a separate 32,000-token allowance; the
whole prompt also contains instructions and schemas. Judge output remains 16,384.
The generation deadline (30 minutes), request timeout (120 seconds), one technical
retry and approved models are unchanged.

The original historical miner JSON/CSV packages and supplied PDF bytes are immutable.
The validator independently parsed and hash-bound its Docling evidence. Ohtsuki's
originally supplied supplement remains in scope; unavailable supplements are not
required. No worked-example gold extraction was shown to the judges.

The r11 Genther attempt is retained as HOLD: consolidation dropped original anchor
IDs despite its retry. The two exact retained failures passed independent source
review once the complete required anchor list was explicit. The fresh r12 run is
reported separately rather than replacing that failed attempt.

Scientific judge accuracy remains unmeasured. This verifies the execution path and
replay, not production qualification; full conformance/pilot work and remaining
production integration are separate.
