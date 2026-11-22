# Package submission and commitment

For daily testnet rounds, use the [testnet participation guide](./0020-testnet-competition-participation.md) for the current service URLs, schedule and runnable submission examples.

Each round publishes its deadline block, allowed models, budgets, and other rules before intake closes. The submitting hotkey signs requests to the [miner upload API](https://artifacts.claims111.ai/docs).

Entry qualification is owned by the backend. Check `/public/competition/entry-policy?network=testnet` for the policy for new rounds, and `/public/competition/rounds/{round_id}/entry-policy` for a round's immutable policy. A testnet waiver removes the prior-consensus-review requirement for package entry; hotkey registration, signed uploads, chain commitments, duplicate checks and team identity rules still apply. Mainnet requires two qualifying mainnet consensus rounds within the seven days preceding the deadline. Consensus reviewer qualification remains separate. Validators cannot request or override an entry waiver.

1. Build the exact wheel or source archive you want evaluated, and record its SHA-256 and byte length.
2. Prepare a `claims_competition_manifest_v1` manifest with its real package filename, entrypoint, declared models, team ID, included source, and production-use licence. See the [sample manifest](https://github.com/DeSciClaims/claims-miner-package/blob/main/examples/competition_manifest.json).
3. Generate a random 32-byte salt. Send the manifest, package hash, size, salt, round ID, and network to `POST /miner/competition/uploads`. Upload the bytes to the returned private staging URL, then sign and send the same body to `POST /miner/competition/uploads/finalize`. The service verifies the bytes and stores a sealed receipt.
4. Publish a Bittensor commitment from the same hotkey before the deadline block. Its two fields are `[Sha256(h), Raw(manifest_id)]`, where `h = sha256(package_bytes || decoded_hotkey_public_key || salt32)` and `manifest_id = sha256(canonical_manifest_json).hexdigest()`. Canonical JSON uses sorted keys, compact separators, and UTF-8 without ASCII escaping. The raw manifest field contains the lowercase 64-character digest as ASCII bytes.

The controller reads the historical commitment at the deadline block and matches it to one sealed upload. Changing package bytes, manifest, hotkey, or salt changes the match. The implementation of the [commitment check](../competition/commitments.py) is the authoritative reference.

## Team identity

Declare the same `team_id` for pipelines controlled by the same operator or team. The manifest records declared affiliation; it does not establish independence. The backend merges entries using shared declarations, shared coldkeys, resolved funding lineage and reviewed code links, even when their declared IDs differ. Advancement and reward limits use these resolved teams: at most two entries per team advance to screen, and one reaches final under the standard stage plan. Code duplicate checks also apply before execution, independently of the declared IDs.

Rounds that publish `policy.code_grouping_policy_sha256` use automatic code-family
grouping. Read the criteria before submission at
`GET /public/competition/rounds/{round_id}/code-grouping-policy`. After subtracting
the listed public starter code and dependencies, sufficient structural overlap
and substantive shared source create a family link. Meaningful variants can still
run, but share one resolved team's advancement and reward allowance. A stricter
duplicate threshold, with matching string literals and supporting resources,
creates a duplicate link for the seeded selection. Similarity percentages alone
are not proof of common ownership or identical behavior. Shared interfaces or
public starter code alone do not establish a family link.

Code-only grouping does not grant the incumbent’s own-version replacement
exemption; that requires declared affiliation, shared coldkeys or resolved funding
links.

The backend derives and freezes these decisions from sealed-package evidence and
its approved policy; a validator cannot choose unpublished thresholds or override
the verdict. Published code verdicts and safe metrics are available at
`GET /public/competition/rounds/{round_id}/code-review` after the verdict release
time. Existing rounds without the policy hash retain their original rules.

Required funding evidence is checked per entry. An unresolved entry may run prescreen but cannot advance; if prescreen is skipped, it is held before evaluation. Other eligible entries continue. The backend records the decision with the round's immutable team evidence, and the public submission list shows the hold reason. Later funding resolution applies to subsequent rounds. If no eligible field remains, the round closes without a contest or new rewards.

## Competition weights

Effective 11 November 2026, competition weights allocate the full vector to the selected production pipeline (70%), the first eligible runner-up (20%), and the second eligible runner-up (10%). Runner-ups must meet the published score band and team rules. A vacant or unqualified slot is burned; its share is not redistributed. The operator receives no competition weight. Published results include the allocation policy and signed weight vector. Before the first competition result, the last signed extraction vector is carried forward with its operator entry removed and the remaining miner shares scaled proportionally to sum to one. Historical scores and already accepted chain weights are unchanged.

## Worker inference

The sample package and the reference pipeline are separate implementations. The generated `claims_reference_competition-0.1.0` wheel is built from the public [reference adapter and build guide](https://github.com/DeSciClaims/claims-reference-miner#competition-baseline-package), using the round's pinned extraction source and reference profile.

The worker request and manifest use the plain OpenRouter catalog model ID, such as `deepseek/deepseek-v4-flash-0731`. The worker supplies `CLAIMS_INFERENCE_BASE_URL` and a paper-scoped `CLAIMS_INFERENCE_TOKEN`. Direct calls to its Bifrost `/chat/completions` endpoint must prefix the wire model with `openrouter/`, for example `openrouter/deepseek/deepseek-v4-flash-0731`. The token's model allowance remains the plain catalog ID. The [sample package](https://github.com/DeSciClaims/claims-miner-package) implements this mapping.

`request.inference.budget` exposes the actual paper allowance: `token_limit` counts input plus output across all calls, and `dollar_limit_usd` is the paper's inference spending limit. `remaining_tokens` and `remaining_dollar_limit_usd` match the gateway key at the start of this attempt, after any earlier attempt's recorded usage. The challenger token limit is derived from baseline usage and bounded by the published ceiling. A retry shares the original budget. These values are an initial allowance, not live counters; packages should track their own usage. Gateway accounting remains authoritative. `inference.max_output_tokens` is the separate per-call output setting.

## Evaluation archives

## Evaluation downloads

After a round is published, its entrant hotkeys can sign `GET /miner/competition/rounds/{round_id}/evaluation-export` at `https://artifacts.claims111.ai`. While the archive is building, the endpoint returns `status: "preparing"`. Once ready, it returns one 10-minute download link and SHA-256 digest for a compressed JSONL archive covering all miners. Verify the digest after download and filter by hotkey locally. Package wheels, execution logs, private worker transport receipts, Gold answer keys, and credentials are excluded.

Scientific schema v1.1 rounds use `claims_competition_evaluation_export_v3` and include:

- `scientific_evaluations`: frozen paper receipts and the hotkey-to-output-alias mapping.
- `scientific_configuration`: the exact evaluation configuration, including its hash.
- `scientific_score_components`: exact G/C/T/P/Q values, applicability and per-task bits. Fractions use `numerator` and `denominator`; `null` means unmeasured or not applicable.
- `scientific_mechanical_findings` and `scientific_worker_validation`: authoritative gate findings and retained worker validation reports, including package failures.
- `scientific_evidence`: original content-addressed bytes for sampling manifests, task cards, assertion packets, verdicts, source evidence, scientific JSON/CSVs, settings and judge run receipts with requests/raw/parsed responses. Decode `body_base64` and verify `sha256` and `byte_length`. References in these records identify related evidence by hash.
- `scientific_evidence_omissions`: explicitly identifies private worker transport receipts excluded from the archive.

All rounds include `scores` and `paired_scores`. Legacy rounds also retain `eligibility_gate_decisions`, `silver_evaluations`, and `silver_coverage_matches` for their Silver candidates, adjudications and coverage matches.

For development rehearsals, use `https://claims-uploads-dev.vercel.app` with the same signed routes and `network="testnet"`. The production and dev services read separate databases; use the service where the round was submitted. The dashboard does not authenticate download requests.
