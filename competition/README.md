# Weekly competition controller components

The controller modules in this directory run outside untrusted miner workers.
They lock submissions, release the sample, run and score paper jobs, tally Gold,
and sign the weekly publication. See the Team vault engineering plan for the
round schedule and repository ownership.

## Early-stage Decision API pilot

The Zils checkpoint named in the reward proposal is unavailable. The
`OpenAIDecisionsRouter` is a temporary trusted-controller adapter for the
pre-screen and screen. It submits only single-candidate cases with source
context to OpenAI Decisions (`gpt-6-luna`); pairs, missing source context, and
choices below the frozen stage threshold go to the two full judges. The router
is never passed to the final or week-one evaluator. Freeze the provider,
model, prompt hash, and thresholds in the weekly configuration. Do not enable
the route for a paid round until its confidence buckets have been measured on
the Claims holdout; the older direct-choice benchmark shows weak rejection
recall. The API key belongs only to the trusted controller.

The final paper scorer now freezes candidate Gold review cases for primary
judge splits with pairwise credit effects. Gold preparation can replay these
private receipts without another stochastic judge run. Applying decided votes
to final scores remains to be implemented.

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
