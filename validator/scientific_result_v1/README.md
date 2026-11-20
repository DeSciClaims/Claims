# Scientific-result validator

This is the shared, read-only validator for scientific extraction schema 1.1.
It checks extraction JSON, delivered table CSVs and controller-owned source
artifacts. It does not repair the submission or trust miner-generated reports.

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
A missing or failed reviewer remains unchecked, not passed. Known unavailable
required sources and omission of a supplied supplement block complete coverage.
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

All four worked examples pass deterministic checks. Offline regression against
the previous generated packages detects incorrect field-to-cell references and
invalid local cores. A controller manifest can additionally block the known
unavailable external Perel table. This is not automatic missing-source discovery.

The new live model-assisted review has not been verified: automatic approval
review rejected transfer of generated extraction packages plus sources for this
new review purpose. Earlier source-transfer permission covered the extraction
benchmark. Offline tests cover successful/rejected full gates, invented evidence,
review failures/budgets, missing sources, forged reports, CSV transport, CLI
behavior and source/package integrity. No scientific performance claim follows
from mocked-review tests.
