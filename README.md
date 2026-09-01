# Claims Subnet

Claims is a Bittensor subnet for extracting scientific contribution claims from
papers and linking each claim to grounded evidence in the source text.

This repository contains the runnable miner, validator, protocol, schemas, and
operator documentation for the Claims subnet.

## What It Does

- Miners receive paper-extraction tasks and return structured claim-evidence
  packets.
- Validators audit miner outputs for source grounding, valid claim-evidence
  links, and coverage of the task scope.
- The neuron entry points expose the miner and validator through Bittensor.

The canonical miner pipeline is `agent_v1`: a skill-capable agent miner that
uses the [ARA](https://github.com/ARA-Labs/Agent-Native-Research-Artifact)
compiler skill and writes Claims-owned structured agent artifacts derived from
the ARA markdown artifact model.
The older `v0` direct model pipeline remains available only as a legacy
compatibility path while the validator and network envelope continue to support
existing Claims v0 tasks.

## Repository Layout

```text
Claims/
├── miner/agent_v1/    # canonical skill-capable agent miner pipeline
├── miner/v0/          # legacy direct claim extraction pipeline
├── validator/agent_v1/# canonical agent artifact validation pipeline
├── validator/v0/      # audit and scoring pipeline
├── neurons/           # Bittensor miner, validator, and protocol
├── schemas/           # shared data contracts
├── docs/              # design notes and operator runbooks
├── examples/          # example papers and inputs
├── requirements.txt
└── .env.example
```

## Service URLs

| Service | URL | Used by |
|---|---|---|
| Claims dashboard | [dashboard.claims111.ai](https://dashboard.claims111.ai) | Public network and miner views |
| Validator/backend API | [api.claims111.ai](https://api.claims111.ai) | Validators, dashboard, and administrative data |
| Validator/backend API docs | [api.claims111.ai/docs](https://api.claims111.ai/docs) | Interactive OpenAPI documentation |
| Miner artifact-upload API | [artifacts.claims111.ai](https://artifacts.claims111.ai) | Signed miner artifact uploads |
| Miner upload API docs | [artifacts.claims111.ai/docs](https://artifacts.claims111.ai/docs) | Interactive OpenAPI documentation |

Set the role-specific backend URL as follows:

```env
# Validator
CLAIMS_BACKEND_URL=https://api.claims111.ai

# Miner
CLAIMS_BACKEND_URL=https://artifacts.claims111.ai
```

## System Prerequisites

The default production validator profile processes 50 papers across 15 miners
and runs diagnostic, reference, and Silver work concurrently. The smaller
requirements below are suitable for installation checks and reduced-concurrency
testing; use the production recommendation for sustained subnet operation.

| Component | Requirement | Notes |
|---|---|---|
| OS | Ubuntu 22.04+ recommended; macOS 13+ for manual development | The Ubuntu installers support Debian-family Linux. Published containers use Ubuntu 24.04. |
| Validator CPU/RAM | 8 cores / 32 GB minimum; 16 cores / 64 GB recommended | The production 50-paper, 15-miner profile runs multiple agent and PDF-processing subprocesses. Reduce worker counts on the minimum configuration. |
| Miner CPU/RAM | 4 cores / 16 GB minimum; 8 cores / 32 GB recommended | Higher paper concurrency starts additional agent and PDF-processing subprocesses. The example miner profile uses two paper workers. |
| Persistent disk | 50 GB miner; 100 GB validator | Allow additional space when retaining many outputs, Bronze references, PDFs, model caches, or logs. Container deployments must persist `/data`. |
| Docker | Docker Engine 24.0+ recommended | Required only for the published container workflow; manual and Ubuntu-installer deployments do not require Docker. |
| Python | 3.10+ | The project enforces Python 3.10 or newer. Published Ubuntu 24.04 images use Python 3.12. |
| Bittensor SDK | `10.5.0` | Installed from `requirements.txt`. Other SDK versions are not supported unless the repository pin is updated and tested. |
| Network | Stable broadband; public TCP port for miners | Miners must expose a reachable Axon. Both roles need outbound access to Bittensor, Claims APIs, PDF storage, and configured inference providers. |
| GPU | Not required with hosted inference providers | Operators using local models must size GPU memory and runtime dependencies for those models separately. |

## Installation

Choose one installation path: manual Python setup, the Ubuntu installers, or
the published Docker images.

### Manual Installation

On Ubuntu or Debian, install the native build and PDF tools first:

```bash
sudo apt-get update
sudo apt-get install -y \
  build-essential ca-certificates curl git \
  libgl1 libglib2.0-0 poppler-utils \
  python3 python3-dev python3-pip python3-venv
```

On macOS, install Python 3.10 or newer and Poppler before continuing. The
Ubuntu installer is the supported automated path for production Linux hosts.

Clone Claims and create its Python environment:

```bash
git clone https://github.com/DeSciClaims/Claims.git
cd Claims
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
python -m pip install --no-deps -e .
```

For a miner, start with the miner environment template:

```bash
cp examples/miner.env.example .env
```

For a validator, install the public reference miner at a reviewed commit and
use the profile for the network you will validate. Install the reference miner
with the same interpreter that will run the validator:

```bash
git clone https://github.com/DeSciClaims/claims-reference-miner.git ../claims-reference-miner
git -C ../claims-reference-miner checkout <PINNED_COMMIT>
.venv/bin/python -m pip install ../claims-reference-miner

# SN111 mainnet profile. Use validator.testnet.env.example for testnet instead.
cp validator/agent_v1/validator.mainnet.env.example .env
```

The default profiles use Hermes. Install it explicitly for a manual setup:

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh -o /tmp/hermes-install.sh
bash /tmp/hermes-install.sh --skip-setup --skip-browser

HERMES_BIN="$(command -v hermes || true)"
HERMES_BIN="${HERMES_BIN:-$HOME/.local/bin/hermes}"
"$HERMES_BIN" --help >/dev/null
printf 'Add this line to .env: HERMES_CMD=%s\n' "$HERMES_BIN"
```

Add the printed `HERMES_CMD` line to `.env`; dotenv does not expand `$HOME`, so
use the absolute path printed by the command. Edit `.env`, set the provider
credential, wallet names, and every model field used by the selected profile.
The production profile is
[validator.mainnet.env.example](./validator/agent_v1/validator.mainnet.env.example);
use
[validator.testnet.env.example](./validator/agent_v1/validator.testnet.env.example)
for testnet. The mainnet template deliberately leaves model fields blank. Each
validator stage passes its provider and model from `.env`; the Ubuntu installer
also writes the corresponding persistent Hermes defaults. Follow the
[Chutes provider configuration](./validator/agent_v1/README.md#chutes-provider)
when using Chutes. Codex and Claude must be installed and authenticated
separately when selected. Protect the completed environment with
`chmod 600 .env`.

Manual installation does not create, fund, or register a Bittensor wallet.
Before launching, create or restore the role's wallet, register its hotkey on
the selected subnet, and set `BT_WALLET_NAME` and `BT_WALLET_HOTKEY` to the
local wallet directory and hotkey filename. Claims reads the wallet from
`~/.bittensor/wallets/`; never place a coldkey in `.env` or in the repository.

Verify the exact runtime and wallet before starting a run. These checks must
all succeed; do not launch a production batch if one fails:

```bash
.venv/bin/python -c 'import claims_reference_miner; print("Reference miner runtime OK")'
"$HERMES_BIN" --help >/dev/null && echo "Hermes runtime OK"

.venv/bin/python -m dotenv -f .env run --override -- sh -c '
  test -n "$BT_WALLET_NAME"
  test -n "$BT_WALLET_HOTKEY"
  test -s "$HOME/.bittensor/wallets/$BT_WALLET_NAME/coldkeypub.txt"
  test -s "$HOME/.bittensor/wallets/$BT_WALLET_NAME/hotkeys/$BT_WALLET_HOTKEY"
  test -x "$HERMES_CMD"
  echo "Validator environment and wallet OK"
'
```

Always load `.env` explicitly and use the same virtual-environment interpreter
for the outer dotenv command and the validator process:

```bash
.venv/bin/python -m dotenv -f .env run --override -- \
  .venv/bin/python -m neurons.validator \
    --netuid <NETUID> \
    --wallet.name <VALIDATOR_WALLET> \
    --wallet.hotkey <HOTKEY> \
    --subtensor.network <NETWORK> \
    --logging.info
```

This requirement also applies to PM2 and systemd. Activating `.venv` in an
interactive shell does not change a service manager's interpreter or `PATH`.
Set the service working directory to the Claims checkout, point it at
`.venv/bin/python`, load `.env` through `python-dotenv`, and set `HERMES_CMD`
to the absolute Hermes path when it is not on the service `PATH`.

### Ubuntu Installers

The public installers set up system packages, Python, Claims dependencies,
Hermes, and a role-specific `.env` template:

```bash
git clone https://github.com/DeSciClaims/Claims.git
cd Claims

./scripts/install-miner.sh

# Validator: copy the detailed profile before installation so Hermes is
# configured from the selected provider and model.
cp validator/agent_v1/validator.testnet.env.example .env
./scripts/install-validator.sh \
  --reference-repo-version <PINNED_COMMIT>
```

They configure Hermes non-interactively from `HERMES_PROVIDER`, `HERMES_MODEL`,
and `HERMES_BASE_URL` in that role's `.env`; provider credentials remain in `.env`.
Chutes is supported as a named OpenAI-compatible provider; see
[Chutes provider configuration](./validator/agent_v1/README.md#chutes-provider).
Hermes is the only external CLI harness installed automatically. Install and
authenticate Codex CLI or Claude CLI separately before selecting either one;
the native DSPy and LangChain harnesses are included with the Python dependencies.
The detailed testnet profile runs one 50-paper bucket-policy cycle against
10-13 miners. The generic
installer template submits weights for four runs, waiting three hours after each
completed run before starting the next one.

They do not create, copy, register, or fund Bittensor wallets. The detailed
profile contains wallet and hotkey names for the container entrypoint, but no
wallet material; change those names for your validator. Create or restore and
register the wallet separately. Manual neuron commands can pass the local names
with `--wallet.name <WALLET_NAME>` and `--wallet.hotkey <HOTKEY_NAME>`.
Bittensor normally reads their files from `~/.bittensor/wallets/`.

The validator installer also checks out and installs the public reference miner.
Pin its commit for a reproducible installation:

```bash
./scripts/install-validator.sh \
  --reference-repo-version <PINNED_COMMIT>
```

The default reference repository is
`https://github.com/DeSciClaims/claims-reference-miner.git`. Use
`--reference-repo-url` only when testing a different public fork.

### Docker

The public images already contain Claims, Python dependencies, Hermes, and the
role-specific runtime. The validator image also contains the pinned public
reference-miner package.

Prepare a local validator environment and persistent data directory:

```bash
cp validator/agent_v1/validator.testnet.env.example .validator.testnet.env
chmod 600 .validator.testnet.env
mkdir -p .container-data/validator
# Edit .validator.testnet.env and set OPENROUTER_API_KEY.
```

Run the validator with the local Bittensor wallets mounted read-only:

```bash
docker pull ghcr.io/desciclaims/claims-validator:edge

docker run --rm -it \
  --name claims-validator \
  --env-file .validator.testnet.env \
  -e CLAIMS_BRONZE_ROOT=/data/bronze \
  -e CLAIMS_OUTPUT_DIR=/data/outputs/validator \
  -v "$PWD/.container-data/validator:/data" \
  -v "$HOME/.bittensor/wallets:/data/bittensor/wallets:ro" \
  ghcr.io/desciclaims/claims-validator:edge \
  validator --logging.debug
```

Use an immutable commit tag instead of `edge` for reproducible deployments.
The miner image is `ghcr.io/desciclaims/claims-miner:<tag>`. For Targon and
other ephemeral rentals, mount persistent storage at `/data`. The
[Targon Container Deployment](docs/0016-targon-container-deployment.md) guide includes a
one-command API-assisted miner and validator bootstrap, a manual first-time setup, and
image update and rollback instructions.

PDF inputs use `pdf-inspector` by default. `GROBID_URL` is only required when
you explicitly choose `--pdf-extraction-method grobid`.

## Run The Miner Locally
Use `agent_v1` for new miner runs. It mounts the ARA compiler skill, runs an
agent loop, validates the structured agent JSON artifact, and records runtime
metadata such as elapsed time, attempts, token usage, and cost when the backend
exposes it.

Compile from a PDF:

```bash
python -m miner.agent_v1 \
  --pdf /path/to/paper.pdf \
  --runtime dspy-react \
  --output-dir miner/agent_v1/outputs/my_run
```

Compile from a text extraction:

```bash
python -m miner.agent_v1 \
  --text /path/to/paper.txt \
  --runtime dspy-react \
  --output-dir miner/agent_v1/outputs/my_run
```

The canonical structured output is:

```text
miner/agent_v1/outputs/<run>/agent_output.json
```

Each run also writes:

```text
miner/agent_v1/outputs/<run>/PAPER.md
miner/agent_v1/outputs/<run>/backend_manifest.json
miner/agent_v1/outputs/<run>/skill_manifest.json
miner/agent_v1/outputs/<run>/agent_validation_report.json
```

### Agent Runtime Options

The direct `miner.agent_v1` CLI still uses low-level runtime names:

```bash
python -m miner.agent_v1 --pdf /path/to/paper.pdf --runtime dspy-react --output-dir miner/agent_v1/outputs/my_run
python -m miner.agent_v1 --pdf /path/to/paper.pdf --runtime langchain-agent --output-dir miner/agent_v1/outputs/my_run
```

For live Bittensor neurons, prefer the higher-level harness/model flags shown
below. They derive the runtime, wrapper, and inner CLI command automatically.
PDF inputs use `pdf-inspector` by default; set `--claims.pdf-extraction-method`
to `pypdf` or `grobid` when comparing readers.

See [miner/agent_v1/README.md](./miner/agent_v1/README.md) and
[miner/agent_v1/wrappers/README.md](./miner/agent_v1/wrappers/README.md) for
the SkillPack contract and lower-level wrapper options.

## Run The Validator Locally

Use `validator.agent_v1` for Claims agent miner outputs. It runs deterministic
structural and grounding checks, then a required agent rigor pass.

```bash
python -m validator.agent_v1 \
  --agent-json outputs/my_run/agent_output.json \
  --source-payload outputs/my_run/source_payload.json \
  --runtime dspy-react \
  --output-dir outputs/my_run_validation
```

See [validator/agent_v1/README.md](./validator/agent_v1/README.md) for backend
configuration and output files.

Legacy v0 miner and validator commands are intentionally kept out of the main
quickstart. Use [docs/0009-v0-miner-validator.md](./docs/0009-v0-miner-validator.md)
only when reproducing older compatibility runs.

## Run A Bittensor Miner

Start a miner neuron after the wallet hotkey is registered on the target subnet:

```bash
python -m neurons.miner \
  --netuid <NETUID> \
  --wallet.name <MINER_WALLET> \
  --wallet.hotkey <HOTKEY> \
  --subtensor.network <NETWORK> \
  --axon.ip 0.0.0.0 \
  --axon.external_ip <PUBLIC_IP> \
  --axon.port 8091 \
  --axon.external_port 8091 \
  --claims.pipeline agent_v1 \
  --claims.agent-harness dspy-react \
  --claims.agent-model openrouter/openai/gpt-5-mini \
  --claims.pdf-extraction-method pdf-inspector \
  --claims.batch-max-workers 2 \
  --claims.output-dir miner/agent_v1/outputs/neuron/testnet
```

`agent_v1` is the default `--claims.pipeline`. To run Hermes Agent CLI:

```bash
python -m neurons.miner \
  --netuid <NETUID> \
  --wallet.name <MINER_WALLET> \
  --wallet.hotkey <HOTKEY> \
  --subtensor.network <NETWORK> \
  --axon.ip 0.0.0.0 \
  --axon.external_ip <PUBLIC_IP> \
  --axon.port 8091 \
  --axon.external_port 8091 \
  --claims.pipeline agent_v1 \
  --claims.agent-harness hermes-cli \
  --claims.agent-model openai/gpt-5-mini \
  --claims.pdf-extraction-method pdf-inspector \
  --claims.batch-max-workers 2 \
  --claims.output-dir miner/agent_v1/outputs/neuron/testnet
```

Supported miner harnesses are `dspy-react`, `langchain-agent`, `hermes-cli`,
`codex-cli`, and `claude-cli`. For normal neuron runs, do not set
`CLAIMS_AGENT_INNER_COMMAND`; the harness/model flags derive it when needed.

Miner batch/PDF knobs:

- PDF reader: `--claims.pdf-extraction-method pdf-inspector|pypdf|grobid` or `SUBNET_CLAIMS_PDF_READER=...`. Default is `pdf-inspector`; `grobid` also needs `GROBID_URL`.
- Batch parallelism: `--claims.batch-max-workers N` or `CLAIMS_MINER_BATCH_MAX_WORKERS=N`. Default is `1`; use `2-3` when the model/provider can handle concurrent papers.
- Batch artifacts: set `--claims.backend-url` or `CLAIMS_BACKEND_URL` to the miner-upload API so full artifacts are uploaded outside dendrite responses.

For batch tasks, miners return one compact `articles[]` item per assigned
paper. `agent_v1` articles carry `agent_output`; the top-level `extraction` and
`source_payload` fields are reserved for single-paper compatibility.

Legacy v0 neuron commands are documented separately in
[docs/0009-v0-miner-validator.md](./docs/0009-v0-miner-validator.md) and should
not be used for new testnet miners.

Use `--subtensor.chain_endpoint <WS_ENDPOINT>` instead of
`--subtensor.network <NETWORK>` when connecting to a custom chain endpoint.

## Run A Bittensor Validator

Start a validator neuron after the validator hotkey is registered and ready to
submit weights. The validator gets paper batches from the Claims backend,
queries miners over Bittensor, runs diagnostic validation, optionally creates
Bronze through the reference miner, runs Silver adjudication, posts records
back to the backend, and then sets weights.

Configure provider credentials, harnesses, and independent model choices in
`.env`; see the [validator configuration reference](./validator/agent_v1/README.md#validator-neuron-configuration).
The command line should carry only the settings that identify and schedule the
validator run.

Start from the profile for the network you are operating:

- [Testnet validator environment](./validator/agent_v1/validator.testnet.env.example)
- [Mainnet validator environment](./validator/agent_v1/validator.mainnet.env.example)

The testnet profile includes an illustrative diverse model set. Mainnet model
fields are deliberately blank and must be selected independently by each
validator operator.

Both profiles currently set `CLAIMS_ALLOW_PAPER_REUSE=true` so the backend may
draw from approved papers that appeared in earlier batches while the paper
catalog is still growing. Canonical task IDs, paper assignments, miner
assignments, and uploaded artifacts remain isolated to their canonical batch.

### Testnet

This focused testnet command requests three papers and one specific miner. Add
`--claims.target-uid` again to include more UIDs, or replace it with bucket
selection as shown in the mainnet command.

```bash
python -m dotenv -f .env run --override -- python -m neurons.validator \
  --netuid 530 \
  --wallet.name <VALIDATOR_WALLET> \
  --wallet.hotkey <HOTKEY> \
  --subtensor.network test \
  --claims.network testnet \
  --claims.backend-url https://api.claims111.ai \
  --claims.batch-size 3 \
  --claims.target-uid <MINER_UID> \
  --claims.audit-method llm \
  --claims.validator-pipeline auto \
  --claims.silver-enable \
  --claims.output-dir validator/agent_v1/outputs/neuron/testnet \
  --claims.timeout 1800 \
  --claims.max-steps 1 \
  --logging.info
```

Primary arguments:

- `--netuid`, `--wallet.name`, `--wallet.hotkey`, and `--subtensor.network`
  identify the subnet, signing wallet, and chain endpoint.
- `--claims.network` selects the `testnet` or `mainnet` backend data partition;
  it does not choose the Bittensor chain endpoint.
- `--claims.backend-url` enables canonical paper/miner assignments, artifact
  reuse, run persistence, and dashboard records.
- `--claims.batch-size` controls papers per batch. `--claims.target-uid` is an
  exact smoke-test override; production uses `--claims.miner-selection-mode
  bucket`. Bucket selection combines eight established-miner seats with up to
  five FIFO newcomers derived from the live metagraph and backend evaluation
  history. A newcomer is an operational coldkey with neither a completed
  evaluation under any hotkey nor a prior canonical qualification assignment;
  its earliest registered serving hotkey represents it.
  `--claims.bucket-max-newcomers-per-batch` controls the newcomer cap and
  defaults to `5`. The established seats are four
  weighted performance draws and four oldest-evaluation rotation seats;
  established miners fill any shortage below ten.
  The legacy `adaptive` mode remains available and uses
  `--claims.miner-sample-size` with a 40/40/20 split.
  The performance lane requires a positive latest evaluation and is weighted
  by `0.10 + mean(last three scores)`. A miner whose latest score is zero is
  excluded from performance and ranks behind miners not yet evaluated.
  A latest score of zero excludes the miner from every lane for
  `CLAIMS_MINER_ZERO_SCORE_COOLDOWN_BLOCKS` (default `7200`, about 24 hours).
  Adaptive draws allow at most one UID per coldkey and reject Axons within
  `CLAIMS_MINER_IPV4_PROXIMITY_ADDRESSES` (default `1024`) IPv4 addresses or the
  same IPv6 `/64`. If the eligible pool lacks enough distinct identities, the
  batch has fewer miners rather than relaxing these caps.
- `--claims.audit-method llm`, `--claims.validator-pipeline auto`, and
  `--claims.silver-enable` enable the current diagnostic and Silver scoring path.
- `--claims.output-dir` stores local run artifacts. `--claims.timeout` is the
  miner-response deadline in seconds.
- `--claims.max-steps` limits completed scoring cycles; `0` runs indefinitely.
  `--claims.query-interval` is the delay between cycles.
- `--claims.force-new-canonical-batch` is a one-shot operator override that
  creates an immediate canonical successor. It requires the signing hotkey in
  the backend's `CLAIMS_BATCH_OVERRIDE_HOTKEY_ALLOWLIST` and does not cancel
  the previous batch.
- `--claims.audit-only` persists proposed scores without setting on-chain
  weights. Omit it for a weight-setting validator.

### Mainnet Baseline

The current SN111 baseline is 50 papers, 10-13 bucket-selected miners, four
cycles separated by a three-hour post-completion delay, a one-hour miner deadline,
mean batch scoring, and bucket-aware weights. Model IDs are intentionally omitted: validators
and miners should configure independent models and credentials privately rather than
converging on a published validator model set.

```bash
python -m dotenv -f .env run --override -- python -m neurons.validator \
  --netuid 111 \
  --wallet.name <VALIDATOR_WALLET> \
  --wallet.hotkey <HOTKEY> \
  --subtensor.network finney \
  --claims.network mainnet \
  --claims.backend-url https://api.claims111.ai \
  --claims.batch-size 50 \
  --claims.miner-selection-mode bucket \
  --claims.batch-score-rule mean \
  --claims.audit-method llm \
  --claims.agent-v1-validation-mode llm \
  --claims.validator-pipeline auto \
  --claims.silver-enable \
  --claims.payout-mode bucket \
  --claims.output-dir validator/agent_v1/outputs/neuron/mainnet \
  --claims.timeout 3600 \
  --claims.max-steps 4 \
  --claims.query-interval 10800 \
  --claims.require-validator-permit \
  --logging.info
```

The private `.env` must still define provider credentials and the harness/model
used for each validation role. Capacity controls, canonical assignment behavior,
diagnostic batching, Silver limits, persistence, and all supported harnesses are
documented in the [validator neuron configuration reference](./validator/agent_v1/README.md#validator-neuron-configuration).

## Suggested Reading

1. [miner/agent_v1/README.md](./miner/agent_v1/README.md)
2. [docs/0011-agent-v1-canonical-miner.md](./docs/0011-agent-v1-canonical-miner.md)
3. [docs/0012-ara-vs-claims-v0-schema.md](./docs/0012-ara-vs-claims-v0-schema.md)
4. [docs/0013-agent-v1-validator-seal-and-benchmarks.md](./docs/0013-agent-v1-validator-seal-and-benchmarks.md)
5. [miner/agent_v1/wrappers/README.md](./miner/agent_v1/wrappers/README.md)
6. [validator/v0/README.md](./validator/v0/README.md)
7. [neurons/README.md](./neurons/README.md)

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md) for the contribution workflow.
