# V1 Consensus Validator

V1 consensus runs as a separate validator process.

## Responsibilities

- The extraction validator continues to build Bronze and Silver records and set
  extraction weights.
- The consensus validator reviews stored adjudication cases later.
- Consensus does not run extraction, set weights, or change extraction rewards.
- The backend owns round composition, leases, hidden answers, scoring, and outcomes.

## Current Round Assembly

- Genuine cases: `100` same-batch adjudications, with every remaining tiebreak
  selected before unanimous fillers.
- Reviewers: `20` serving miners.
- Consensus quorum: `11` qualified coldkey groups.
- Deadline: `1800` seconds.
- Batches with more than 100 tiebreaks produce multiple round sequences.

The backend materialization cron discovers completed extraction batches and
materializes tiebreak cases in resumable FIFO pages. A preparation cron adds only
the unanimous cases needed for full rounds, prepares the hidden tests, and
freezes each round sequence. Validators can claim only complete prepared rounds.

## Review Flow

- Every case includes its own source-document metadata.
- Reviewers download each paper and extract their own source payload.
- Responses include a selected option, confidence, rationale, and quoted
  evidence.
- The backend checks quotes against the corresponding complete Bronze source.
- Miners upload signed responses to the miner-upload API.
- Dendrite returns only the immutable submission manifest.
- Hidden test answers never leave the backend.

Only reviewers scoring at least `0.75` on the current round's hidden test cases
contribute votes to genuine-case outcomes. A normal non-response scores zero;
validator or system failures can void an assignment instead.

## Reviewer Independence

- Exclude miners from the genuine source extraction batch.
- Exclude sibling hotkeys under the same coldkey.
- Exclude miners in the same resolved pre-registration funding-lineage cluster.
- Admit at most one reviewer from each known lineage.
- Fall back to exact coldkey separation for unresolved identities.

All cases are scoped to one extraction batch, so the round's extractor
exclusions cover every represented source.

## Extraction Gate

`CLAIMS_CONSENSUS_EXTRACTION_GATE=true` is enabled by default.

- Never-reviewed miners remain provisionally eligible.
- One completed review uses that score.
- Two completed reviews use their mean.
- Three or more use the latest-three mean.
- The qualifying mean is at least `0.75` without display rounding.

## Run The Validator

```bash
python -m dotenv -f .env run --override -- python -m neurons.consensus_validator \
  --netuid 111 \
  --wallet.name <VALIDATOR_WALLET> \
  --wallet.hotkey <HOTKEY> \
  --subtensor.network finney \
  --claims.network mainnet \
  --claims.backend-url https://api.claims111.ai \
  --logging.info
```

Important validator settings:

- `CLAIMS_CONSENSUS_DEADLINE_SECONDS=1800`: miner response deadline.
- `CLAIMS_CONSENSUS_LEASE_SECONDS=2100`: worker lease with finalization grace.
- `CLAIMS_CONSENSUS_QUERY_TIMEOUT`: timeout for each backend-configured
  assignment.
- `CLAIMS_CONSENSUS_QUERY_WORKERS=20`: parallel reviewer requests.
- `CLAIMS_TARGET_UIDS`: optional smoke-test restriction to specific UIDs.
- `CLAIMS_CONSENSUS_INTERVAL`: delay between polling cycles.
- `CLAIMS_CONSENSUS_EXTRACTION_GATE=true`: apply consensus qualification to
  extraction selection.

## Miner Settings

Consensus review uses structured DSPy calls. It inherits the extraction provider
and model unless consensus-specific overrides are set.

- `SUBNET_CLAIMS_CONSENSUS_PROVIDER`
- `SUBNET_CLAIMS_CONSENSUS_MODEL`
- `SUBNET_CLAIMS_CONSENSUS_API_BASE`
- `SUBNET_CLAIMS_CONSENSUS_API_KEY_ENV`
- `SUBNET_CLAIMS_CONSENSUS_MAX_TOKENS`
- `SUBNET_CLAIMS_CONSENSUS_TIMEOUT`
- `SUBNET_CLAIMS_CONSENSUS_BATCH_SIZE`
- `SUBNET_CLAIMS_CONSENSUS_MAX_WORKERS`
- `SUBNET_CLAIMS_CONSENSUS_SOURCE_MAX_WORKERS`

See [the miner configuration](../miner/agent_v1/README.md#v1-consensus-review)
for details.
