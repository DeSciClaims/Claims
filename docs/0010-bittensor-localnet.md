# Bittensor Localnet Operation

This guide runs the Claims miner and validator on a Bittensor subnet. The miner
defaults to the canonical `agent_v1` ARA pipeline; the validator currently uses
the v0 compatibility audit stack. The same neuron scripts accept localnet,
testnet, and mainnet subtensor endpoints; localnet is the recommended
development target.

## Install

From the `Claims/` directory:

```bash
pip install -r requirements.txt
```

The neuron runtime requires the Bittensor Python SDK. If the SDK is not already
installed in the active environment:

```bash
pip install bittensor
```

Set the LLM environment used by `miner.agent_v1`:

```bash
cp .env.example .env
```

Fill in `OPENROUTER_API_KEY`. `GROBID_URL` is only needed for legacy
`miner.v0` runs that use `--pdf-extraction-method grobid`.

## Start Localnet

The localnet image needs enough memory to instantiate the runtime. With Colima:

```bash
colima stop
colima start --memory 8 --cpu 4 --disk 20
docker pull ghcr.io/opentensor/subtensor-localnet:devnet-ready
docker run -d \
  --name local_chain \
  --restart unless-stopped \
  -p 9944:9944 \
  -p 9945:9945 \
  ghcr.io/opentensor/subtensor-localnet:devnet-ready
```

If Colima reports `lima not found` even though Homebrew installed it, run Colima
with Homebrew on `PATH`:

```bash
PATH=/opt/homebrew/bin:/opt/homebrew/sbin:$PATH colima start --memory 8 --cpu 4 --disk 20
```

If Colima repeatedly fails with `failed to run attach disk "colima", in use by
instance "colima"`, delete and recreate the Colima profile. This resets local
Docker images and containers stored inside Colima:

```bash
colima delete --force
colima start --memory 8 --cpu 4 --disk 20
```

In another terminal:

```bash
btcli subnet list --network ws://127.0.0.1:9945
```

Stop and restart the same container with:

```bash
docker stop local_chain
docker start local_chain
```

Replace the container only when you want a fresh local chain:

```bash
docker stop local_chain
docker rm local_chain
docker run -d \
  --name local_chain \
  --restart unless-stopped \
  -p 9944:9944 \
  -p 9945:9945 \
  ghcr.io/opentensor/subtensor-localnet:devnet-ready
```

## Wallets

Create or restore the local Alice wallet, then create separate role wallets:

```bash
btcli wallet create --uri alice

btcli wallet create --wallet.name sn-creator --hotkey default
btcli wallet create --wallet.name test-validator --hotkey default
btcli wallet create --wallet.name test-miner --hotkey default
```

List the coldkey addresses:

```bash
btcli wallets list
```

Fund each role wallet from Alice. Replace each destination with the coldkey
address printed by `btcli wallets list`:

```bash
btcli wallet transfer \
  --wallet.name alice \
  --destination <SN_CREATOR_COLDKEY_SS58> \
  --amount 10000 \
  --network ws://127.0.0.1:9945

btcli wallet transfer \
  --wallet.name alice \
  --destination <VALIDATOR_COLDKEY_SS58> \
  --amount 1000 \
  --network ws://127.0.0.1:9945

btcli wallet transfer \
  --wallet.name alice \
  --destination <MINER_COLDKEY_SS58> \
  --amount 1000 \
  --network ws://127.0.0.1:9945
```

## Subnet

Create the subnet:

```bash
btcli subnet create \
  --subnet-name claims \
  --wallet.name sn-creator \
  --wallet.hotkey default \
  --network ws://127.0.0.1:9945 \
  --no-mev-protection
```

Find the subnet id:

```bash
btcli subnet list --network ws://127.0.0.1:9945
```

Start emissions:

```bash
btcli subnet start \
  --netuid 2 \
  --wallet.name sn-creator \
  --network ws://127.0.0.1:9945
```

Register the miner and validator hotkeys:

```bash
btcli subnet register \
  --netuid 2 \
  --wallet-name test-miner \
  --hotkey default \
  --network ws://127.0.0.1:9945

btcli subnet register \
  --netuid 2 \
  --wallet-name test-validator \
  --hotkey default \
  --network ws://127.0.0.1:9945
```

## Prepare a PDF URL Task

The network-facing task input is a downloadable PDF URL. The validator sends the
URL, and each miner chooses how to download, parse, cache, and extract from the
paper.

For one task, pass the URL directly to the validator:

```bash
--claims.paper-url https://example.org/paper.pdf
```

For multiple tasks, create a JSONL manifest:

```json
{"task_id":"claims_task_001","paper_url":"https://example.org/paper.pdf","source_sha256":""}
```

Then pass:

```bash
--claims.task-manifest examples/tasks/localnet_urls.jsonl
```

Artifact mode is still available for deterministic smoke tests. For legacy v0
validator compatibility tests, create an artifact by running the v0 miner once
in file mode:

```bash
SUBNET_CLAIMS_RUN_LABEL=claims_v0 python -m miner.v0 \
  --pdf /path/to/paper.pdf \
  --pdf-extraction-method grobid
```

The artifact will be written under:

```text
miner/v0/outputs/section_context_v1__run_claims_v0/<paper_id>/artifact.json
```

## Run the Miner

Start the miner in one terminal:

```bash
python -m neurons.miner \
  --netuid 2 \
  --wallet.name test-miner \
  --wallet.hotkey default \
  --subtensor.chain_endpoint ws://127.0.0.1:9945 \
  --axon.port 8091 \
  --claims.agent-runtime dspy-react
```

Use a different port for each miner process. `agent_v1` is the default
`--claims.pipeline`. To run the legacy miner instead, pass
`--claims.pipeline v0 --claims.pdf-extraction-method grobid`.

## Run the Validator

Start the validator in another terminal:

```bash
python -m neurons.validator \
  --netuid 2 \
  --wallet.name test-validator \
  --wallet.hotkey default \
  --subtensor.chain_endpoint ws://127.0.0.1:9945 \
  --claims.paper-url https://example.org/paper.pdf \
  --claims.task-id claims_v0_localnet \
  --claims.audit-method deterministic
```

For a single validation round:

```bash
python -m neurons.validator \
  --netuid 2 \
  --wallet.name test-validator \
  --wallet.hotkey default \
  --subtensor.chain_endpoint ws://127.0.0.1:9945 \
  --claims.paper-url https://example.org/paper.pdf \
  --claims.task-id claims_v0_localnet \
  --claims.audit-method deterministic \
  --claims.max-steps 1
```

Use `--claims.audit-only` to write validator audits without submitting weights.
Use `--claims.task-artifact` instead of `--claims.paper-url` when you want to
debug with a prebuilt local artifact.

## Other Networks

Change only the subtensor endpoint and netuid:

```bash
python -m neurons.miner \
  --netuid <NETUID> \
  --wallet.name <WALLET> \
  --wallet.hotkey <HOTKEY> \
  --subtensor.network test
```

```bash
python -m neurons.validator \
  --netuid <NETUID> \
  --wallet.name <WALLET> \
  --wallet.hotkey <HOTKEY> \
  --subtensor.network finney \
  --claims.paper-url https://example.org/paper.pdf
```

Use funded, registered wallets for the target network. Mainnet miners also need
a reachable public axon:

```bash
python -m neurons.miner \
  --netuid <NETUID> \
  --wallet.name <WALLET> \
  --wallet.hotkey <HOTKEY> \
  --subtensor.network finney \
  --axon.port 8091 \
  --axon.external_ip <PUBLIC_IP> \
  --axon.external_port 8091
```

## File-Based Smoke Tests

The canonical agent miner can be tested without starting subtensor:

```bash
python -m miner.agent_v1 --help
python -m miner.agent_v1 \
  --text examples/agent_v1_smoke.txt \
  --runtime dspy-react \
  --output-dir /tmp/claims-agent-v1-smoke
```

The legacy v0 pipeline and validator can also be tested without starting
subtensor:

```bash
python -m miner.v0 --help
python -m validator.v0 --help
python -m validator.v0 \
  --extraction-output-json miner/v0/outputs/section_context_v1__run_claims_v0/Rietveld_et_al_2013_Science/section_context_v1_output.json \
  --extraction-run-id claims_v0_smoke \
  --audit-method deterministic \
  --output-dir /tmp/claims-v0-validator-smoke
```

## Outputs

Miner neuron outputs are written under:

```text
miner/v0/outputs/neuron/<task_id>/<paper_id>/
```

Validator neuron audits are written under:

```text
validator/v0/outputs/neuron/<task_id>/uid_<uid>/
```
