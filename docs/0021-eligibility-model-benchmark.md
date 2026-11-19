# Eligibility model benchmark

Benchmark eligibility models outside competition rounds, using saved production
requests and the existing DSPy runtime, Pydantic contracts, evidence validation
and optional production retry/split recovery. It does not write to the backend,
change round rules, run miner packages or set weights.

The versioned configuration lives in the infra repository at
`config/competition/benchmarks/eligibility.json`. It contains the DeepSeek V4 Flash
0731 control, GLM 5.3 Flash, MiniMax M3.1 Flash and Qwen3.8 Flash. All settings,
samples, concurrency levels and price ceilings are configurable. Empty
`request_options` preserves the production runtime's absence of an explicit
reasoning override; add a supported `reasoning` or `reasoning_effort` option to
test a different setting. No provider is pinned. Token-price ceilings filter
the automatic OpenRouter routes, so those ceilings are part of the experiment.

## Prepare the private corpus

Run on a trusted machine with saved Silver workspaces. Export the original
`executions/*/task.json` and `output_schema.json`, including skill instructions,
span maps and full evidence. Do not reconstruct requests from a binary decision
dataset or truncate the evidence. The exporter supports compact/verbose primary
judges, blind reconstruction and appellate requests. Recovery requests are
excluded by default; use `--include-recovery` to examine them explicitly.

```bash
python -m neurons.eligibility_benchmark prepare \
  --source /var/lib/claims-competition/testnet/rounds/silver-workspaces \
  --corpus outputs/eligibility-benchmark/packets.jsonl
```

Packets are deduplicated and content-addressed, including their output schema
hash. The saved schema must match the replay runtime; use a matching Claims
checkout for historical workspaces. Samples interleave output
contract, role, candidate cardinality and evidence-size strata deterministically.
Models receive the same samples in individual sweeps. Mixed sweeps distribute
packets round-robin across the configured models. A sample is capped by actual
corpus size; the plan shows requested and available counts. Inspect the corpus
manifest to confirm that both roles, single/pair cases, blind/appellate requests
and long evidence are represented before interpreting results.

## Plan and run

From the Claims checkout, with the infra checkout beside it:

```bash
python -m neurons.eligibility_benchmark plan \
  --config ../infra/config/competition/benchmarks/eligibility.json \
  --corpus outputs/eligibility-benchmark/packets.jsonl --phase compatibility
```

Planning needs no credentials and makes no inference calls. `run` also only
plans unless `--live` is explicit. For live work, copy the configuration locally,
set a positive `budget_usd`, and provide `OPENROUTER_API_KEY` through the shell's
secure environment. The runner reads provider key-limit metadata before
inference. The profile permits an existing key and enforces the local reservation
cap. Set `require_provider_key_limit: true` to additionally require a non-resetting
provider key with remaining credit no greater than `budget_usd`. The checked-in
configuration leaves the budget unset.

The plan shows the minimum budget needed to reserve all concurrent requests.
Live execution rejects a concurrency level whose reservations exceed the budget,
so a spending guard cannot silently make a 16-worker test behave like a smaller
test. Corpus size and wall-time limits still bound the actual load achieved.

`wall_time_seconds` is a separate overall deadline. The CLI supervises a dedicated
runner process and terminates it when that deadline is reached, even if an SDK
call or adapter retry is still waiting. Pending trials become interrupted and
their unpriced HTTP reservations remain charged to the budget. Exit code 124
and `stopped_by_deadline` distinguish that outcome from a completed benchmark.
Individual SDK timeouts are not assumed to be absolute wall-clock deadlines.

```bash
python -m neurons.eligibility_benchmark run \
  --config outputs/eligibility-benchmark/config.json \
  --corpus outputs/eligibility-benchmark/packets.jsonl \
  --output outputs/eligibility-benchmark/run-01 --phase compatibility --live
```

Start with compatibility, then remove failed candidates in a new configuration
and output directory before larger runs. Select `--phase reliability`,
`--phase capacity`, or `--phase mixed`; multiple `--phase` arguments are allowed.
Compatibility/capacity report unassisted failures. Reliability/mixed reuse the
production recovery methods and count the additional calls. The runner disables
DSPy response caches and observes physical HTTP attempts, including adapter
fallbacks, through the existing production runtime. It is a dedicated process;
never import the HTTP interception context into a validator service.

Each HTTP attempt reserves `request_reserve_usd` before transmission. A
conservative byte-based prompt bound and output ceiling must fit the reservation;
OpenRouter requests also carry the configured token-price ceilings. Measured
provider cost replaces the reservation when available. Unpriced, timed-out and
interrupted attempts retain their reservations, including after a restart.
Exhausted reservations stop new work. **This local guard is not a provider billing
guarantee:** a provider key spending limit can supply a second bound.
The report distinguishes measured cost from unknown reserved exposure.

Rerun the identical command to resume. Configuration, corpus and runtime hashes
must match. Completed trials are skipped; an interrupted trial with an existing
wire receipt is recorded as interrupted and is not automatically purchased
again. Budget/interrupted trials and local transport/cost-guard failures are
excluded from model reliability rates and reported separately.
Re-running one of those trials requires a deliberate new run directory. Phase
time limits pause across restarts, but throughput windows include restart gaps;
use an uninterrupted capacity sweep for production sizing. A phase stops at its
request count or duration limit, so a configured 30-minute mixed phase may finish
earlier. A small successful pilot is not evidence of a production SLA.

## Results and quality review

`report.json` and `report.md` show schema and semantic validity, first-attempt and
eventual completion, unresolved cases, retries, 429/5xx/timeouts, truncation,
latency, token/request throughput, observed providers and measured/unpriced cost.
First-attempt validity requires one observed HTTP request and a valid result;
internal DSPy repairs cannot make a model appear reliable on its first attempt.
OpenRouter's hidden internal provider fallbacks are not visible to this client.
The JSON report also separates primary negative/positive, blind and tiebreak
roles. Sampling balances strata rather than reproducing production frequencies;
the overall percentage is not a production-weighted error-rate estimate.
Reports show planned and finished trials and remain marked partial until the
selected work finishes. Run `report` during execution for a current snapshot;
HTTP attempts without a finished trial may still be in flight. Adding another
configured phase on resume extends the recorded plan.

```bash
python -m neurons.eligibility_benchmark report \
  --output outputs/eligibility-benchmark/run-01 \
  --labels outputs/eligibility-benchmark/reference-labels.jsonl
```

Optional private labels have the form
`{"packet_id":"<hash>","decisions":{"k0":"a","k1":"neither"}}`.
Values are `a`, `b` or `neither`, following each saved case's candidate ordering.
Labels are read only by reporting and never passed to a model. Missing decisions
count against agreement. Reports also count false accepts, false rejects and
wrong pair preferences against those labels. Historical judge agreement is not
independent human accuracy; review a paper-disjoint labelled disagreement sample
before selecting production judges. Blind reconstruction needs separate review.

Raw replies, usage and request receipts remain in the private output directory
(directories 0700; files 0600). Authentication headers are never saved. Do not
commit corpora, responses, labels or local credential files. Publish only reviewed
aggregate reports. No inference benchmark has run merely because a plan exists.
The run also saves its runtime source snapshot and dependency versions for replay
and audit, including uncommitted benchmark implementation files.
