# Simplified validation experiment

This is a separate local DSPy diagnostic. It does not create competition receipts,
ranking rows or weights, and it does not change the existing scorer.

The process has three checks:

1. **Structure:** reuse the deterministic package validator for JSON, graph and CSV contracts.
2. **Completeness:** create one source-only catalogue of findings, sample from it,
   and compare those findings and sampled table rows with the complete submission.
3. **Support:** sample submitted scientific records and check their assertions and
   necessary relationships against the captured source.

There is one first-pass scientific judge. A different model independently reviews
every failure, unresolved response and a seeded 10% sample of passes. The reviewer
does not see the first response. Agreements settle the check; disagreements remain
explicitly unresolved. There is no arbiter or repeated evidence-expansion loop.

Every scientific request receives all captured Docling text, the original
extraction JSON and all delivered CSVs. The experiment refuses a request that
cannot fit the selected model; it never truncates the source or submission.
Code assigns bounded integer reference codes. Judges select codes instead of
generating JSON paths, source IDs or result identifiers. Their scientific decisions
and witnesses remain model-authored.

The initial experiment reuses a trusted earlier independent Docling capture bound
to the same supplied PDF hashes. It does not reuse the old generated targets,
eligibility labels, matches or judgments. The capture must be validator-owned;
this script is not an intake endpoint for untrusted capture bundles.

## Sampling and score interpretation

The frozen diagnostic policy samples four source findings per prose/table stratum,
five source rows per numerical table and eight submitted scientific root records
(or the whole population when smaller). Source findings are created without seeing
the miner's submission. C balances available prose/table strata; T balances tables;
P averages sampled assertion-record outcomes. All required scientific dimensions
are considered jointly, and a failed check contributes zero.

The formula is the familiar `G * P * (7C + 2T) / 9`, with absent source components
removed and weights renormalized. **These sampling populations differ from the
production guide. This diagnostic Q is not interchangeable with competition Q.**

Completed checks persist individually. An unresolved check stays in its original
denominator: lower bounds treat it as a failure and upper bounds as a pass. A final
diagnostic Q is emitted only when all checks resolve (or the package gate fails).
Unknowns are never silently excluded. Technical failures are retained with errors,
not relabeled as unsupported science. The report shows each target, both judgments,
evidence references, coverage and the resulting score/bounds.

## Run

Use an explicit local proprietary diagnostic profile with the three role keys
`SOURCE_READER`, `PRIMARY`, `SECONDARY`, a shared uncapped accounting ledger and
`max_in_flight` no greater than 20. The two scientific judges must use different
models. Each profile declares its context and output allowance, reasoning setting
and supported parameter omissions. Supply saved model/price snapshots.

```sh
python scripts/evaluate_scientific_simple.py \
  --bundle /trusted/inputs.jsonl \
  --capture-run /trusted/earlier-independent-Docling-run \
  --profiles /local/diagnostic-profiles.json \
  --model-snapshots /local/model-snapshots.json \
  --sampling-secret-file /protected/sampling-secret.bin \
  --env-file /protected/model-credentials.env \
  --output /local/new-experiment
```

Only the OpenRouter model credential is read from the environment file. Provider
routing is automatic; no provider is pinned. Requests, native responses and usage
are retained using the existing recorded DSPy runtime and concurrency dispatcher.
There is one response-validation retry. Valid completed operations are reused only
when their input, instructions and model profile fingerprints match.

`manifest.json` freezes experiment inputs, sampling policy, model roles and code
identity. `plan.json` freezes checks before candidate judging. `evidence.json`
contains the original JSON/CSV and source/reference maps. `report.md` is the readable
per-check outcome; `report.json` retains machine-readable decisions and bounds.

Execution and contract tests do not establish scientific accuracy. This experiment
still needs source-grounded human review and broader evaluation before any
competition policy change.
