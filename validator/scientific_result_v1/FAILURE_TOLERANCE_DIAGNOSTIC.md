# Original validator with failure tolerance

The historical trial below used 50%. The subsequent owner-approved first-round
competition policy uses **80% as an advisory completion target**, with conservative
scores retained below it. See [First-round scoring](FIRST_ROUND_SCORING.md).

This local DSPy diagnostic keeps the original guide workflow: source preparation,
independently reviewed source cards, matching, complete scientific packets,
primary/secondary judging and arbitration. It changes continuation and reporting,
not the scientific criteria or the source/table/bucket sample populations.

The owner-approved trial threshold is **50% resolved weight separately in each
applicable C, T and P component**. This is distinct from the round's eligible-paper
minimum. A scientific PASS or FAIL is a completed evaluation. Network errors,
invalid responses, timeouts, malformed references, source ambiguity, packet limits,
unexpected exceptions and exhausted retries are unresolved work.

One failed source-card build does not stop other selected regions, T checks or P
audits. One failed matching/judging case does not stop independent cases or other
entrants. A failed prerequisite blocks only its actual dependents: unknown source
identity is not invented, failed table correspondence does not become a missing
table allegation, and unknown audit occupancy does not become an empty P=1.
If a whole common prerequisite fails, the report still emits a conservative zero
with an upper bound of one, zero coverage and an unaccepted status. That zero is
not a measurement of miner quality.

Every unknown planned scientific unit remains in its original denominator. Failed
card construction leaves the selected source tile unknown, regardless of how many
cards it might have produced. Failed case shards leave their parent case unknown.
The projection preserves guide averaging across cards, source tiles, source tables
and assertion buckets. Mandatory unanchored failures retain their P=0 effect.

The conservative score uses zero credit for unknown work. The upper bound assumes
unknown work passes. Both use the original G/C/T/P formula. The acceptance flag is
based on exact fractional resolved coverage, not scientific pass rate. Scores are
reported even below threshold, marked insufficiently evaluated. A gate-unknown
package earns no demonstrated credit; a confirmed invalid package remains G=0.

This projection is **not an official competition score**. The strict guide receipt
continues to be HOLD with null Q when incomplete. Production adapters cannot consume
the separate diagnostic as a valid published score. Promoting this policy requires
an explicit competition policy and guide revision.

## Run

```sh
python scripts/evaluate_scientific_saved_paper.py \
  --bundle /trusted/inputs.jsonl --output /local/new-experiment \
  --harness dspy --failure-tolerance \
  --minimum-resolved-fraction 1/2 --diagnostic-workers 20 \
  --model SOURCE_READER=qwen/qwen3.8-flash \
  --model PRIMARY=qwen/qwen3.8-flash \
  --model SECONDARY=deepseek/deepseek-v4-flash-0731 \
  --model ARBITER=moonshotai/kimi-k2.5 \
  --sampling-secret-file /protected/sampling-secret.bin \
  --env-file /protected/model-credentials.env
```

The experiment freezes its policy and source/output identities. Prior stopped runs
are not resumed under changed rules. The original open-model DSPy route parameters
remain explicit: 16,384 maximum output tokens, a 120-second per-attempt request
deadline and one controller retry. The paper clock is two hours, and at most twenty
native requests can run concurrently. Provider routing remains automatic.

`failure-tolerance.md` shows the score/bounds, threshold decision, stage completion,
logical-operation success rates, failed attempts and costs. `failure-tolerance.json`
contains the event and attempt ledgers. Logical completion, provider-attempt success
and weighted scientific coverage use separate denominators: a retry that succeeds
is one completed operation and still has a recorded failed attempt. Do not add
these overlapping failure counts together.

Fault-injection integration tests cover a common source-card network failure,
one candidate's scientific-request network failure and invalid response contracts.
They verify that unrelated T/P work continues, other entrants retain their results,
partial coverage is counted, the conservative score is numeric, and official
competition adapters still receive no measured scores from a HOLD receipt.
