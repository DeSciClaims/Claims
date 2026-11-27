# Scientific source preparation scheduler

`scripts/prepare_scientific_sources.py` prepares the already revealed source
sample independently of miner execution and scientific scoring. It queues
prescreen, screen and final in the order declared in the frozen round plan,
using sample position within each stage. Each completed source becomes available
to workers immediately; future stages can prepare before their entrants advance.

The scheduler calls the existing frozen scientific policy and `prepare_paper`.
It uses the original PDF hashes, parser-cache directories, preparation clocks,
source commitments and prepared-source heads. Existing source records are reused.
It does not modify the round rules, sample, miner outputs, judge configuration,
scores or stage advancement. It does not construct a reference answer.

Concurrency comes from the round's published `bronze_max_workers` setting
(currently 10). Requests use the existing host-wide shared LLM gate. At most
that many source jobs are submitted at once, in stage order. Individual failures
are reported with exception type or hold category only; other preparations
continue. Submitted source evidence and model messages are not printed.

Run under the existing trusted service identity and protected environment:

```sh
python scripts/prepare_scientific_sources.py \
  --round-id round_20261125T150000Z_mainnet \
  --backend-url https://api.claims111.ai --poll-seconds 30
```

Keep the frozen Claims runtime on `PYTHONPATH`. This scheduling entrypoint is
separate from the scientific runtime: changing scheduling does not require a
scientific-policy revision or regeneration of completed source commitments.
It still verifies the frozen scientific runtime before preparing sources.
Do not run two preparation schedulers for the same round simultaneously.

SIGTERM/SIGINT stop new submissions and drain current work. Give the systemd unit
at least the frozen preparation deadline plus transport time before forced kill.
The package worker and scientific controller are separate services and remain up.
The previous preparation unit should be retained for rollback.

## Terminal preparation exclusions

A source that cannot finish preparation before its original two-hour deadline
can receive an operator-reviewed `claims_source_preparation_exclusion_v1` record.
It binds the frozen PDF hash, configuration, expired clock, failure diagnostics
and actor. The database refuses exclusions after successful preparation, a score,
an evaluation, or attempted package execution. Existing prepared/scored papers
are preserved. The scheduler skips these terminal dispositions.

Every entrant, including the baseline, receives the same unmeasured disposition.
Queued jobs close as **not executed**; future-stage jobs are prevented from
running. There is no scientific Q, mechanical failure or worker execution receipt
for an excluded paper. Infrastructure-marked compatibility pair rows close the
original grid but contribute nothing to any entrant's average. Excluded papers
remain in the original planned sample when calculating the stage's eligibility
minimum. The approved mainnet minima remain 10/20, 60/120 and 250/500.

The API exposes separate prepared, excluded, executed and scored counts. The
authenticated round evaluation archive retains the exclusion records, original
clock, and explicitly referenced source-review diagnostics. Late preparation is
rejected rather than silently changing the common comparison.

For an already frozen generator, install the separately hash-attested
`scripts/scientific_preparation_exclusion_reader.py` adapter alongside its existing
replay reader. It verifies the named experimental round/configuration, exact
all-entrant pair disposition and receipt/clock hashes. It preserves completed
scientific receipts and uses the original ranking, team and tie rules. It also
omits unexecuted jobs from code-runtime and blocked-egress audits; actual worker
executions retain their existing security checks.

## Preserving a seed across an implementation-only repair

A prepared paper can retain its original sampling commitment when an explicitly
approved implementation repair changes the policy fingerprint without changing
scientific settings. `claims_seed_policy_continuation_v1` binds the original and
current policies, preparation, source frame, seed hash and sealed output pool.
The original seed bytes and commitment remain unchanged. Models, prompts,
parser, sampling, weights, limits and all other policy fields must match; only
the recorded factory and implementation identities may differ.

The backend validates this record before accepting its immutable authorization
head. Evaluation, receipt replay and independent publication all verify the same
contract. Authenticated evaluation exports include the authorization and its
policy evidence without exposing the preparation's private seed blob. Existing
judging clocks and completed scores are retained.

For a frozen runtime, `scripts/scientific_seed_continuation_reader.py` installs
separately hash-attested evaluator/replay copies only for the approved paper
and policy. Other papers use the original evaluator. Final receipt verification
can run in bounded parallel batches; every original verification still runs and
any failure propagates. Installing the adapter leaves the frozen generator
files and their runtime descriptor unchanged.

## Publication after evaluation

New scientific rounds created through `prepare_round` publish as soon as final
scoring, integrity checks, winner selection and the duplicate-dispute window are
complete. Their creation-time `publish_at` is the earliest instant after that
window, rather than the old weekly review delay. Existing frozen schedules are
not reinterpreted, and production activation remains a separate scheduled gate.

For an existing completed round, the backend operator command
`scripts/amend_publication_time.py` previews an owner-only publication amendment.
It can advance only `policy.schedule.publish_at`, after the dispute window has
closed. It retains the scientific configuration hash, source commitments, jobs,
scores and stage receipts, and records the prior/new rules in append-only audit
history. The controller still runs every final publication check.
