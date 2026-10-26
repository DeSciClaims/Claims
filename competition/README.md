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
one package and one PDF. The guest verifies all hashes before it installs the
package; `worker_guest` then invokes `run_one_paper` and writes a result and
full install/execution log. The transport must keep the input directory
unwritable by the miner user, provide a pinned offline wheelhouse, and run the
guest harness as an unprivileged user. The scoped token is transferred
separately and never enters the durable bundle.
`PinnedSshWorkerTransport` implements this transfer for a prepared VM. It
requires a preverified SSH host key, stages bundle files with read-only modes
under a separate OS owner, sends the scoped token on stdin, runs the guest
harness with an empty environment as `claims-miner` in a transient systemd unit
with process-group cleanup and resource limits, fetches bounded result/log
bytes, and removes its job directory. Before staging, it checks the miner UID, exact
`nftables` allowlist, and gateway hostname resolution; a missing or changed
policy fails closed. The pilot SSH user needs passwordless sudo on this
credentialless VM; production should restrict that account to a fixed worker
control command. The image also needs a pinned runtime/wheelhouse and a host
firewall allowing the miner process to reach only the inference endpoint.
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
those writes. Both steps require the same frozen package bytes and any reviewed
duplicate-link evidence.
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
private owned file descriptors, verifies the package hash, and returns bytes
to a trusted production runner without executing them. It can read handoffs
and rollbacks atomically; the production runner and health checks remain to be
wired.
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
statistics. Review the sealed source and this evidence, then provide a private
review JSON with `round_id`, `evidence_sha256`, `reviewer`, and one package row
per finalist: `hotkey`, `package_sha256`, and Boolean `checks` for
`hardcoded_output`, `evaluation_context`, `cap_evasion`, and `fragmentation`.
Run the same command with `--review-file PATH` to write `<round_id>.audit.json`.
The `build` result is derived from trusted jobs and cannot be overridden.
The controller recomputes the evidence and rejects a stale or altered review;
absent or mismatched files stop the transition. These source-behavior checks
still need reviewer judgment until calibrated automatic detectors are added.
The controller checks voter
qualification against backend consensus records and payout UIDs against the
current metagraph. Automating reviewer judgments and checking live archive
and signer compatibility remain integration work.
The competition_round_setup CLI opens intake from an operator draft. It
reconstructs the provisional judge runtime, verifies sealed baseline and
fallback package bytes through Hippius, checks the Monday 12:00 UTC / D+600
calendar, writes matching private and public rules JSON, then creates the
backend round with the exact rules body and its hash. The backend serves
`GET /public/competition/rounds/{round_id}/rules` before the submission
deadline; the dashboard links to it. A different rules body cannot replace
an opened round.
The frozen configuration also hashes the competition policy, stage, Gold,
copy, weight, and worker source modules. The controller checks that fingerprint
before scoring; the worker checks it before leasing a job. A midweek code
change therefore stops that round until the pinned build is restored.
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
receipts; automatic detection of hard-coded outputs, context gaming, cap
evasion and fragmentation still needs calibration.
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
production service still needs a reader that notices the pointer, starts the
selected package in its isolated runtime, and reports health before launch.

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
