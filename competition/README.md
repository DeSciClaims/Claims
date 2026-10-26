# Weekly competition controller components

The controller modules in this directory run outside untrusted miner workers.
They lock submissions, release the sample, run and score paper jobs, tally Gold,
and sign the weekly publication. See the Team vault engineering plan for the
round schedule and repository ownership.

`BittensorArchive` supplies historical block hashes, raw commitment metadata,
and block timestamps from the archive-node Subtensor SDK. The SDK's decoded
commitment string is unsuitable for the two-field package commitment.
`HippiusPrivateStorage` reads sealed package objects against their receipt
hashes and writes scoped, checksum-tagged private worker logs. It belongs only
in trusted controller or coordinator processes.
`CompetitionPaperInputs` reads the Bronze-backed pool captured atomically at
intake lock. The backend excludes papers without a matching complete Bronze
source, pins each Bronze and full-source ID, and rejects a reveal outside the
locked pool or with a different pool hash. The controller can obtain the pinned
PDF URL and SHA-256, Bronze artifact, and source-span context for scoring.
`CompetitionPackageResolver` checks a leased job against its frozen stage
entry and the selected upload receipt. `TrustedPdfFetcher` reads a pinned PDF
through an allowlisted HTTPS host or a private Hippius S3 client, bounds its
size, checks its SHA-256, and gives only the bytes to the disposable worker.
`run_next_claimed_job` joins these inputs to the existing per-paper Bifrost
key, execution, log, and completion lifecycle.
It now reads the selected upload's manifest and the pinned Bronze paper
metadata before making a worker contract. The Bifrost paper key is restricted
to the models declared by that manifest, after checking the frozen whitelist.
`build_worker_bundle` produces deterministic `input.json`, `request.json`,
one package under its real pip filename, and one PDF. The guest verifies all hashes before it installs the
package; `worker_guest` then invokes `run_one_paper` and writes a result and
full install/execution log. The transport must keep the input directory
unwritable by the miner user, provide a pinned offline wheelhouse, and run the
guest harness as an unprivileged user. The scoped token is transferred
separately and never enters the durable bundle.
`RestrictedSshWorkerTransport` is the default prepared-VM transport. It
requires a preverified SSH host key and sends one tar stream to the VM's
root-owned `claims-worker-control run` command. The dedicated `claims-control`
SSH account has only that sudo permission. The helper checks the exact
`nftables` allowlist and pinned gateway address, stages read-only bundle files,
and passes the scoped key to an unprivileged `claims-miner` systemd unit with
resource limits. It rejects miner-created symlink outputs, returns bounded
result/log bytes, and removes its job directory. The image needs a pinned
runtime/wheelhouse and a host firewall allowing the miner process to reach
only the inference endpoint. The earlier `PinnedSshWorkerTransport` remains
for pilot compatibility but is not used by the worker factories.
The `miner` and `miner.agent_v1` package exports are lazy, so the guest harness
loads the canonical Pydantic artifact schema without loading Claims' full
PDF/model runtime.
The Targon Workloads API does not supply an SSH host-key fingerprint, so the
transport cannot safely connect to a newly provisioned VM until that key has
been authenticated through a separate trusted channel. A prepared pilot VM
can use an operator-verified known_hosts entry; automated fresh-VM transport
remains an integration gate.
`TargonVmLifecycle` follows Targon's documented register/deploy/state/delete
API. It creates a separate VM for a job, enforces an hourly price ceiling,
and deletes a failed provision. `DisposableVmExecution` also deletes the VM
after worker transport returns or raises. Automated fresh-VM use still needs
an authenticated SSH host-key source and a preconfigured guest image. The
prepared-VM SSH transport has local tests; live isolation remains unverified.

After the deadline lock, `reveal_selected_sample` verifies the D+600 drand
beacon, uses its seed for the duplicate lottery, and publishes exactly the
papers required by the surviving entrant count. `start_revealed_round` replays
that draw, checks any previously stored duplicate verdicts, writes missing
verdicts, and starts the first stage. It is safe to retry after a crash between
those writes. Both steps require the same frozen package bytes and reviewed
duplicate-link evidence. After intake locks, first create the private
`<round_id>.duplicate-candidates.json` review queue with
`python -m neurons.competition_duplicate_candidates --round-id ID --rounds-dir DIR --backend-url URL`.
Pass each approved public starting package with `--public-base-archive PATH`.
The bounded scanner reads sealed archives without installing them, records
renamed-code structure and shared prompts/resources, subtracts identical public
base files, and compares every qualified package with every other one and the
incumbent. Scan errors are recorded per package. Similarity is review evidence,
not an automatic exclusion threshold; approve links only after human review.
Then create `<round_id>.duplicates.json`
with `python -m neurons.competition_duplicate_review --round-id ID --rounds-dir DIR --backend-url URL --links-file LINKS.json`.
The input file is `{"links":[{"left":"hotkey-a","right":"hotkey-b","reason":"reviewed match"}]}`;
use an empty list when no additional links were found. The receipt binds the
review to the locked archive block, baseline package, qualified package hashes,
and complete candidate report. Reviewed
links may point to the frozen incumbent hotkey. A new version from the incumbent's
team must use a separate qualified hotkey because stage jobs are keyed by hotkey. The backend
freezes its private link list and publishes the hash with the sample. A restarted controller
checks both before starting a stage. Links cannot be supplied in the
pre-deadline round draft.
The round's public policy must freeze `duplicate_verdict_public_at` and
`duplicate_dispute_window.opens_at`/`closes_at` as timezone-aware ISO
timestamps after the submission deadline. The verdict release must be no later
than the dispute window opening. Public intake masks duplicate status and
evidence until that release time.
During that window a hotkey-signed miner can appeal one duplicate exclusion per
team through `claims-miner-uploads`. The backend stores the private reason and
an immutable `pending`, `upheld`, or `overturned` review. An overturned appeal
corrects the public record only; the stage entrants and scores remain frozen.
All appeals must be resolved before the signed final result, which includes
their public summary. The backend checks the summary against its appeal ledger.
Reviewers use the controller-authenticated `GET
/internal/competition/rounds/{round_id}/duplicate-disputes/private` to read
the private reason, then `POST
/internal/competition/rounds/{round_id}/duplicate-disputes/review` with
`{"hotkey":"...","status":"overturned","public_explanation":"..."}`
(`upheld` is the other status).
The explanation is public; it must not quote private code.
The week-one reference launch releases exactly 320 papers; later staged finals
require at least 500. For later weeks the controller computes the final count
from the frozen weekly dollar budget and the number of distinct challengers
after the duplicate draw, using the proposal's pre-screen, screen, and final
cost formula. A budget that cannot fund 500 final papers fails before release.
With one or two challengers, the single stage is capped at 1,000 papers.
Each paper job has one retry after an expired lease or a reported
infrastructure fault. A second failure is terminal; scoring drops the affected
paper for every package. A stale worker cannot complete it, and each attempt
has a distinct private log object.
`ProductionReleaseReader` opens the active release pointer and package through
private owned file descriptors, verifies the package and manifest hashes, and
returns the release to a trusted production runner without executing it. It can read handoffs
and rollbacks atomically. `run_production_paper` checks a leased PDF and the
active release, gives only a paper-scoped key to the disposable VM, revokes it,
checks the structured output, and records private logs and usage. It performs
no Bronze comparison or competition scoring. The backend code now includes a
durable production lease and result store; a separate worker polls those jobs.
The worker polls approved papers after a configured start timestamp and
enqueues them for the active release. Queued work can switch to a new release
after handoff. Product materialization, exhausted-job recovery, and health
checks remain to be built and rehearsed.
Before advancing a staged screen or opening Gold, the controller counts
incumbent package failures on papers without a global infrastructure fault.
The final selector repeats the check. A strict rate above 20% starts a durable
reference rerun on the same sample. The controller requeues the challengers
against the pinned reference package, scores the rerun, and then advances or
opens Gold. A final rerun can publish a signed `reference_fallback` decision
with the production reward share burned if no challenger wins. This path has
local SQLite and PostgreSQL tests but still needs a live worker and handoff
rehearsal.

`weekly_automation.tick_round` is the trusted timer entrypoint. Each call
reads the durable round state and performs one transition: deadline lock,
D+600 release, first-stage start, ready-paper scoring and advancement, Gold
opening or closing, or signed Thursday publication. A process scheduler must
call it repeatedly and provide archive, paper-pool, Bronze, judge, audit, and
signer adapters. It reports `missed_gold_window` if final scores arrive after
Thursday 12:00 UTC; it will not publish a crown without the Gold vote.
`service_loops.run_controller_cycle` and `run_worker_cycle` read every
unpublished round through the controller-authenticated backend API. The
controller advances each once per cycle; a worker claims at most one job per
active round. They return redacted status records and let another round
continue after one fails. The controller's credentialed adapter factory and
service deployment wiring remain.
`python -m neurons.competition_service` is the polling process. It runs in
controller or worker mode, reads the backend token only from its environment,
and loads a trusted `competition.*` adapter module from the Claims checkout.
`--once` runs one cycle for a local rehearsal. `pilot_worker_factory` composes
the worker's Hippius, Bifrost, Targon, PDF and pinned SSH adapters from host
settings and a round JSON file under `CLAIMS_COMPETITION_ROUNDS_DIR`. The file
contains `round_id`, the full public `configuration`, its SHA-256, and sealed
`baseline_upload` / optional `reference_upload` receipts; it contains no
credentials. The factory checks its configuration hash against the backend
before it creates privileged adapters. Fresh Targon VMs still need an
authenticated SSH host-key source for this factory to execute a job.
`pilot_controller_factory` is the matching trusted controller adapter module.
It reconstructs the provisional Decisions and J1/J2 runtime from the round
file, checks its hash, and binds the archive node, validator signer, paper
pool, and sealed packages. It loads `<round_id>.voters.json` only when Gold
opens and `<round_id>.audit.json` only before publication. The voter file
names qualified hotkeys and team IDs. Run
`python -m neurons.competition_code_audit --round-id ID --rounds-dir DIR --backend-url URL`
after the final job grid completes. It writes a private
`<round_id>.audit-evidence.json` containing sealed source fingerprints, build
outcomes, gateway usage, denied egress, and claim-count/repeated-output
statistics. Mainnet rules must publish `policy.code_audit` with
`min_completed_papers`, `max_repeated_output_fraction`,
`max_cap_trip_fraction`, and `max_claim_count_ratio`. When the final grid is
ready, the trusted controller writes the evidence and `<round_id>.audit.json`
automatically; the CLI can also prepare the same receipt. It checks embedded sample IDs and PDF
hashes, evaluation-context literals, denied egress and cap trips, repeated
output sets, and claim-count ratio. The controller recomputes the verdicts
from the sealed evidence and frozen thresholds. Calibrate the thresholds on
week −1 perturbation and baseline runs before a paid round.
To compare candidate thresholds with independently reviewed week −1 controls
and deliberate attacks, run
`python -m neurons.competition_audit_calibration --input private-cases.json --output aggregate-report.json`.
The input is a private JSON object with `schema_version` set to
`claims_code_audit_calibration_v1`, a proposed `policy.code_audit` body under
`policy`, and `cases`. Each case has a unique `case_id`, one frozen
`audit-evidence.json` body under `evidence`, and `expected_checks` keyed by
finalist hotkey, each with Boolean `build`, `hardcoded_output`,
`evaluation_context`, `cap_evasion`, and `fragmentation` labels. The report
counts false rejections and missed attacks for each check, and shows whether
the observed numeric controls and attacks can be separated by an upper
threshold. It contains only aggregate counts and input/policy hashes, not
package names or source. Keep the labeled input private; review the report
and freeze chosen thresholds in the public rules before mainnet.
For a testnet round without automatic thresholds, review the sealed source and this evidence, then provide a private
review JSON with `round_id`, `evidence_sha256`, `reviewer`, and one package row
per finalist: `hotkey`, `package_sha256`, and Boolean `checks` for
`hardcoded_output`, `evaluation_context`, `cap_evasion`, and `fragmentation`.
Run the same command with `--review-file PATH` to write `<round_id>.audit.json`.
The `build` result is derived from trusted jobs and cannot be overridden.
The controller recomputes the evidence and rejects a stale or altered review;
absent or mismatched files stop the transition. Mainnet cannot use this manual
receipt path.
The signed weekly result publishes the evidence SHA-256 and each finalist's
number of failed checks, while package bytes and detailed findings stay private.
The controller checks voter
qualification against backend consensus records and payout UIDs against the
current metagraph. Calibrating thresholds and checking live archive and signer
compatibility remain integration work.
The competition_round_setup CLI opens intake from an operator draft. It
reconstructs the provisional judge runtime, verifies sealed baseline and
fallback package bytes through Hippius, checks the Friday 12:00 UTC / D+600
calendar, writes matching private and public rules JSON, then creates the
backend round with the exact rules body and its hash. The backend serves
`GET /public/competition/rounds/{round_id}/rules` before the submission
deadline; the dashboard links to it. A different rules body cannot replace
an opened round.
The draft's public policy must include `reference_lock_sha256`, the hash
recorded in Bronze manifests for the chosen reference release. Intake selects
only Bronze records with that hash, and the private paper read checks it again.
The frozen configuration also hashes the competition policy, stage, Gold,
copy, weight, and worker source modules. The controller checks that fingerprint
before scoring; the worker checks it before leasing a job. A midweek code
change therefore stops that round until the pinned build is restored.
For an independent replay, configure a distinct
`CLAIMS_COMPETITION_REPLAY_TOKEN` on the backend. A second validator can read
`GET /internal/competition/replay/rounds/{round_id}/snapshot` only after the
sample release, plus `/paper-pool` for the complete frozen draw pool and
`/bronze/{stage}/{paper_id}` for the same pinned Bronze
artifact and PDF URL. The snapshot contains every qualified locked upload,
including duplicate exclusions, with commitment metadata and sealed object
keys, but no private Gold seed or answers. The replay principal cannot call
primary-round mutation routes and needs separate read-only Hippius access to
fetch package and PDF bytes. `CompetitionReplayInputs` validates the public
rules schema and hash, binds the snapshot's deadline, network, reference
release and package hashes back to those rules, and checks reviewed-link
hashes. `build_replay_initial_plan` checks the
archive-node commitment for each qualified package, redraws the sample from a
separately verified drand beacon and hash-checked full paper pool, reproduces the
duplicate lottery, and derives the first-stage job grid. It needs a separately
transferred baseline package receipt whose bytes match the public hash.
`initialize_replay_runtime` combines these reads with the independent archive
and beacon check, then writes the verified plan into the local replay store.
The store treats the primary runner's later `evaluating` or `finished` status
as the original accepted upload, so a second validator can restart or begin
after primary execution without changing its locked package identities.
After Gold voting closes, `/gold-review` serves public case prompts, the
voter roster, and signed votes, but no private answers or check seed. The
replay client verifies every vote signature, case, team, and exclusion before
returning the closed record. It can independently tally the primary check
cases, including a zero-case finalist. Applying those votes to an
independently generated Silver case set remains a separate policy step.
`align_closed_gold_prompts` measures exact one-to-one prompt overlap and
leaves changed or ambiguous cases unmatched; it does not transfer decisions.
`ReplayStore` persists that grid, leases baseline jobs before challengers,
records worker results, and freezes independent per-paper adjudications and
pair scores. `ReplayRuntimeBackend` combines only those local writes with
read-only Bronze and PDF inputs, so existing worker and scorer components can
be reused. `create_replay_worker_coordinator` connects the existing paper
worker to this private ledger; `advance_ready_stage` can select and enqueue
later stages from its own pair scores. The mocked worker test covers one
paper through execution and scoring, and a restart test covers screen-to-final
advancement. `tick_replay` is a restartable controller step that scores ready
local papers, advances the stage, handles an incumbent failure by rerunning
the same papers against the frozen reference, and stops at a complete final
score grid. Rerun Bronze reads use the original stage even when the primary
validator did not rerun it. Gold integration and its own signed vector still
need implementation; the current
components alone are not an independent validation.
`select_replay_final` can apply the usual crown rules to the local complete
pair grid and frozen local audit once a complete, policy-approved Gold outcome
for the local finalists is supplied. It does not infer one from primary votes.
`python -m neurons.competition_replay_service --role controller --round-id ID
--backend-url URL --db-path PATH` runs the replay scoring step repeatedly;
`--role worker --worker-id ID` runs one local paper job per poll. `--once`
performs one step. The dedicated `replay_pilot_factory` bootstraps both roles
from a separately verified archive and drand beacon, the read-only replay API,
and a private 0600 `<round_id>.json` file under
`CLAIMS_COMPETITION_REPLAY_ROUNDS_DIR` containing `round_id`,
`baseline_upload`, and optional `reference_upload`. It needs
`CLAIMS_COMPETITION_REPLAY_TOKEN`, `CLAIMS_COMPETITION_NETUID`, the archive,
Hippius, Bifrost, Targon, worker SSH, PDF allowlist, and provisional judge
settings used by the corresponding pilot factories. Set
`CLAIMS_HIPPIUS_REPLAY_LOG_BUCKET` to a separate bucket: replay job IDs can
match primary job IDs, so their private logs must not share an object keyspace.
After the local final score grid is complete, the controller applies the
frozen automatic code thresholds, output-copy check, and denied-egress audit
to its own job and Silver receipts and freezes a private final audit. A
testnet round without a code policy stops at
`ready_for_independent_final_review`; a round with one reports
`final_audit_complete`. Gold review and a second signed vector remain to be
built.
Pilot judge providers are set with CLAIMS_JUDGE_J1_A_PROVIDER,
CLAIMS_JUDGE_J1_B_PROVIDER, CLAIMS_JUDGE_J2_A_PROVIDER,
CLAIMS_JUDGE_J2_B_PROVIDER, and CLAIMS_JUDGE_TIEBREAK_PROVIDER. Round setup
requires all five pins. Each OpenRouter request uses only its frozen provider
slug and disables provider fallback. The pins must be chosen after checking
live model endpoints and cost; placeholder slugs are only valid for tests.
For the final stage, it now recomputes each finalist's output-copy verdict
from the frozen private Silver receipt and paired score grid. The 85% accepted
unit overlap and two-standard-error gain rule is applied against the baseline,
and the evidence is included in the signed public result. The final code-audit
evidence collector now binds review to measured worker and sealed package
receipts; the automatic thresholds for hard-coded outputs, context gaming, cap
evasion and fragmentation still need week −1 calibration.
The disposable worker firewall also counts denied packets for the miner UID.
The trusted SSH transport checks the reviewed two-rule policy before and after
each run, records blocked attempts even when the guest exits unsuccessfully,
and treats any denied egress as a package fault. The final controller recomputes
per-hotkey counts from private worker receipts and blocks a finalist with any
denied egress; it stops publication for a baseline violation. This is one
observable code-integrity check, not a substitute for package and behavioral
audits.

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
recall. `FrozenEvaluationPolicy` checks the local configuration hash and
published routing fields before constructing stage evaluators, then keeps
Decisions out of the final and week-one stages. The API key belongs only to
the trusted controller.

The post-deadline seed assigns each screen and final paper J1 or J2. Each
configuration is a separate two-pass panel; only the assigned panel runs on a
paper. A split within that panel can invoke the frozen tiebreak. Pre-screen
uncertain cases use J1. `judge_runtime_config` puts each pass's implementation,
profile, model runtime, model name, endpoint and inference parameters into the
weekly configuration hash. `silver_runtime_config` adds hashes of the Silver
prompt/scorer source files and the embedding model, endpoint and thresholds.
Neither descriptor contains a credential value. `FrozenEvaluationPolicy`
checks both before each paper and Gold known-answer audit, and fails closed if
embeddings are enabled without a trusted controller credential. The embedding
credential can be a scoped gateway key; the controller does not need the
upstream OpenRouter key. The embedding provider remains shared across J1/J2;
different providers by judge would need a separate scoring-path change.
`build_weekly_judges` assembles two explicit
allowlisted model-backed panels with one common tiebreak through the existing
Silver judge factory. `provisional_judges.py` defines the pilot selection:
J1 = Qwen3 32B + gpt-oss-120b; J2 = DeepSeek V3.2 + Llama 4 Maverick;
shared tiebreak = Qwen3 235B A22B Instruct 2507. All use the OpenRouter
chat-completions endpoint, temperature 1, and 8192 maximum output tokens.
`build_provisional_judges()` is opt-in; it reads the controller's
`OPENROUTER_API_KEY` but never puts it in the frozen configuration. Measure
quality, cost, provider behavior, and context truncation before a paid round.
The factory accepts a trusted usage sink for recording judge model calls.
`build_provisional_evaluation_policy()` binds those judges, the selected
Bronze source provider, the weekly dollar budget, and the worker model/token/
lease limits into one configuration hash. `CompetitionControllerInputs` checks
that hash against the backend round before every source, package, judge, or
audit read. `CompetitionWorkerCoordinator` uses the same frozen worker policy
when it leases and runs a paper through the disposable VM adapter. The
remaining voter and code-audit providers must be supplied by trusted services.
OpenRouter may route one model ID to multiple providers, so model-ID hashing
alone does not freeze the actual serving endpoint. Pin a tested provider and
deploy artifact before intake.

For each paper key, the controller reads Bifrost's final token and dollar
usage before revoking it. The private completed-job receipt includes
`inference_cost_usd`; the paper score stores the same measured value in its
public-detail field. The public score API exposes only worker token and dollar
usage from that field, never its other contents.
Publication signs a backend-recomputed sum by stage and for the round.
The sum covers completed worker job receipts only. It excludes judge and
Decision API calls, VM time, storage, and spend on expired attempts. A live
Bifrost quota-shape and billing reconciliation test is required before treating
this as an operational cost ledger.

`planned_production_handoff` verifies the signed Thursday result, the frozen
finalist package hash and the following Monday 09:00 UTC gate. The trusted
`FilesystemProductionReleaseStore` can atomically store the sealed package and
switch a private `active.json` pointer; it records the previous release and
can restore it after a breach without replaying the breached result. The
trusted `activate_published_round` coordinator resolves the unique winning
sealed upload through the existing backend and Hippius adapters. The
production service still needs a runner that starts the selected package in
its isolated runtime, records each paper's extraction, and reports health before launch.

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
result. The frozen tiebreak judge also evaluates the same sealed cases after
vote closure; its accuracy and vote hashes appear beside voter accuracy. The
prompt uses a verified source span from the pinned Bronze full
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
