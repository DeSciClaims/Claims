# Weekly competition controller components

The controller modules in this directory run outside untrusted miner workers.
They lock submissions, release the sample, run and score paper jobs, tally Gold,
and sign the weekly publication. See the Team vault engineering plan for the
round schedule and repository ownership.

After the deadline lock, `reveal_selected_sample` verifies the D+600 drand
beacon, uses its seed for the duplicate lottery, and publishes exactly the
papers required by the surviving entrant count. `start_revealed_round` replays
that draw, checks any previously stored duplicate verdicts, writes missing
verdicts, and starts the first stage. It is safe to retry after a crash between
those writes. Both steps require the same frozen package bytes and any reviewed
duplicate-link evidence.

`weekly_automation.tick_round` is the trusted timer entrypoint. Each call
reads the durable round state and performs one transition: deadline lock,
D+600 release, first-stage start, ready-paper scoring and advancement, Gold
opening or closing, or signed Thursday publication. A process scheduler must
call it repeatedly and provide archive, paper-pool, Bronze, judge, audit, and
signer adapters. It reports `missed_gold_window` if final scores arrive after
Thursday 12:00 UTC; it will not publish a crown without the Gold vote.

## Early-stage Decision API pilot

The Zils checkpoint named in the reward proposal is unavailable. The
`OpenAIDecisionsRouter` is a temporary trusted-controller adapter for the
pre-screen and screen. It submits only single-candidate cases with source
context to OpenAI Decisions (`gpt-6-luna`); pairs, missing source context, and
choices below the frozen stage confidence threshold go to the two full judges. The router
is never passed to the final or week-one evaluator. Freeze the provider,
model, prompt hash, and thresholds in the weekly configuration. Do not enable
the route for a paid round until its confidence buckets have been measured on
the Claims holdout; the older direct-choice benchmark shows weak rejection
recall. The API key belongs only to the trusted controller.

The final paper scorer freezes contested claim cases, including primary judge
splits with pairwise credit effects. The backend commits to a private Gold
check seed when the round is created, before any package is evaluated. Gold
preparation independently draws the check from contested claims and caps the
split-case escalation pool using the public sample seed. After voting closes,
the signed result reveals the Gold seed and candidate IDs; the backend verifies
the draw against frozen paper receipts. Decided escalation votes rescore final
pairs, including accepted units and rejected miner claims whose votes reverse
the tiebreak. Check cases alone supply the crown veto. Hidden known-answer
cases from the certified consensus bank are mixed into Gold when at least 40
unused cases match the exact final PDF versions. Their answer keys stay sealed
until closure, then voter and decided-vote accuracy is included in the signed
result. The prompt uses a verified source span from the pinned Bronze full
source; cases without that evidence are excluded. Gold waits if the bank cannot supply 40 matching cases. Provisioning
those cases for each fresh final sample and live end-to-end execution remain.

## Weight heartbeat

After publication, a dedicated validator-wallet process reads the newest
public signed receipt, verifies its signature and weekly role shares, resolves
hotkeys against the current metagraph, and calls Bittensor `set_weights`
(including its commit-reveal path). It retains the last verified receipt on
disk when the backend is unavailable or the next result is late. A missing
payee UID burns that slot; an unregistered operator or an unverified UID 0
stops submission. It polls every minute and submits when a subnet tempo has
elapsed, with an earlier 1,800-block ceiling to stay below the 5,000-block
validator cutoff.

Run the consensus validator with `--claims.external-competition-weights` so
its per-batch scoring does not submit competing weight vectors. Then start the
separate process using the same validator wallet:

```sh
python -m neurons.competition_weights \
  --backend-url https://claims.example \
  --network mainnet --netuid 111 --subtensor-network finney \
  --wallet-name VALIDATOR_WALLET --wallet-hotkey VALIDATOR_HOTKEY \
  --operator-hotkey OPERATOR_SS58 \
  --cache-path /var/lib/claims/competition-weight-receipt.json
```

Use `--once --audit-only` to check the latest receipt and current UID mapping
without a transaction. The process has not been run against a live chain or
deployed; its signature, fallback, projection, and cadence paths have local
tests.
