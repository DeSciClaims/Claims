# Bittensor Localnet Operation

This guide runs the Claims v0 miner and validator on a Bittensor subnet. The
same neuron scripts accept localnet, testnet, and mainnet subtensor endpoints;
localnet is the recommended development target.

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

Set the LLM and parser environment used by `miner.v0`:

```bash
cp .env.example .env
```

Fill in `OPENROUTER_API_KEY`. Set `GROBID_URL` if using a GROBID service other
than `http://localhost:8070/`.

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

## Prepare a Task Artifact

The validator sends miners an `artifact.json` containing the paper and source
spans. You can create one by running the v0 miner once in file mode:

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
  --axon.port 8091
```

Use a different port for each miner process.

## Run the Validator

Start the validator in another terminal:

```bash
python -m neurons.validator \
  --netuid 2 \
  --wallet.name test-validator \
  --wallet.hotkey default \
  --subtensor.chain_endpoint ws://127.0.0.1:9945 \
  --claims.task-artifact miner/v0/outputs/section_context_v1__run_claims_v0/<paper_id>/artifact.json \
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
  --claims.task-artifact miner/v0/outputs/section_context_v1__run_claims_v0/<paper_id>/artifact.json \
  --claims.task-id claims_v0_localnet \
  --claims.audit-method deterministic \
  --claims.max-steps 1
```

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
  --claims.task-artifact /path/to/artifact.json
```

Use funded, registered wallets for the target network.

## File-Based Smoke Tests

The core v0 pipeline can be tested without starting subtensor:

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
