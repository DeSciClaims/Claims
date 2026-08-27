# Targon Container Deployment

The Claims images keep software in an immutable image and runtime state on one
persistent volume mounted at `/data`. Replacing a Targon rental therefore does
not remove wallets, environment files, Hermes state, model caches, Bronze
artifacts, outputs, or logs.

## Images

| Target | Contents | Registry visibility |
|---|---|---|
| `miner` | Claims, Python dependencies, Hermes | Public |
| `validator` | Miner image contents plus the public reference miner package | Public |

Never add `.env`, wallets, API keys, Git credentials, or other secrets to either
image. Runtime credentials and wallets belong on the persistent `/data` volume.

## Build Locally

```bash
docker buildx build \
  --file docker/Dockerfile \
  --target miner \
  --tag claims-miner:local \
  --load .

docker buildx build \
  --file docker/Dockerfile \
  --target validator \
  --build-context claims_reference_miner=../claims-reference-miner \
  --tag claims-validator:local \
  --load .
```

Pin Hermes in repeatable builds with
`--build-arg HERMES_COMMIT=<verified-commit>`.

## Publish With GitHub Actions

The `Container images` workflow builds both images on pull requests and
publishes both on relevant `main` and tag changes. It can also be run manually.

Configure these repository settings first:

| Type | Name | Value |
|---|---|---|
| Variable | `CLAIMS_REFERENCE_MINER_REF` | Pinned reference miner tag or commit |
| Variable | `HERMES_COMMIT` | Pinned Hermes commit |

Set both GHCR packages to public so miners and validators can pull them without
registry credentials. Deploy immutable SHA tags rather than `edge` for
reproducible rentals and rollback.

## First-Time Bootstrap

A new Targon volume is empty. It does not contain a Bittensor wallet or a
role-specific environment file, and Targon does not currently document a volume
file-upload or secret-file mount API. The workload API can create and update the
rental, but private files still need a secure transport into the mounted volume.

The recommended helper automates the complete first deployment. It uses the
[Targon workload API](https://docs.targon.com/api/workloads) to create an idle
rental and persistent volume, streams the wallet and environment over Targon's
SSH tunnel, and then updates the workload to start the node automatically. For
a miner, it also creates a direct TCP Axon port and configures the public IP
reported by Targon.

The environment contents and private wallet are never sent through the Targon
API or stored in the workload template.

Prepare the profile for the role and confirm that its wallet names match the
local wallet directory.

Validator:

```bash
cp validator/agent_v1/validator.testnet.env.example .validator.testnet.env
chmod 600 .validator.testnet.env
# Set OPENROUTER_API_KEY, BT_WALLET_NAME, and BT_WALLET_HOTKEY.
```

Miner:

```bash
cp examples/miner.env.example .miner.testnet.env
chmod 600 .miner.testnet.env
# Set OPENROUTER_API_KEY, BT_WALLET_NAME, and BT_WALLET_HOTKEY.
```

For an unattended long-running validator, use:

```env
CLAIMS_MAX_STEPS=0
CLAIMS_QUERY_INTERVAL=21600
```

`CLAIMS_MAX_STEPS=0` means unlimited cycles. A positive value makes the
validator exit after that many cycles, which can cause a rental restart policy
to start it again.

Export a Targon token and organization slug. The helper accepts either
`TARGON_API_TOKEN` or the official `TARGON_API_KEY` name:

```bash
export TARGON_API_TOKEN=<TARGON_TOKEN>
export TARGON_ORG=<ORGANIZATION_SLUG>
```

Run the validator bootstrap from the Claims repository:

```bash
python3 scripts/bootstrap_targon_node.py \
  --role validator \
  --name claims-validator-testnet \
  --image ghcr.io/desciclaims/claims-validator:<COMMIT_TAG> \
  --resource-name <TARGON_RESOURCE_NAME> \
  --env-file .validator.testnet.env \
  --wallet-dir ~/.bittensor/wallets/claims-test-validator \
  --ssh-private-key ~/.ssh/id_ed25519
```

For a miner, use the public miner image and miner wallet. The helper exposes a
direct TCP port and sets `BT_AXON_EXTERNAL_IP` from Targon's workload state:

```bash
python3 scripts/bootstrap_targon_node.py \
  --role miner \
  --name claims-miner-testnet \
  --image ghcr.io/desciclaims/claims-miner:<COMMIT_TAG> \
  --resource-name <TARGON_RESOURCE_NAME> \
  --env-file .miner.testnet.env \
  --wallet-dir ~/.bittensor/wallets/claims-test-miner \
  --ssh-private-key ~/.ssh/id_ed25519 \
  --axon-port 8091
```

The matching `.pub` file is registered with Targon automatically if necessary.
Use `--ssh-key-uid <UID>` to reuse a registered key. By default, the helper
creates a 20 GB volume using Targon's `storage-rentals` volume resource. Supply
`--volume-uid <UID>` to bootstrap an existing empty volume, or
`--volume-resource-name <NAME>` when your organization uses a different storage
resource.

Use `--workload-uid <UID>` to repurpose an existing suspended or registered
rental instead of allocating another server. The helper verifies that the
rental uses the requested resource, refuses to modify a running workload,
clears any previous command override, and configures the Claims image in idle
mode before uploading private state. Unless `--volume-uid` is also supplied, a
new persistent volume is created for the reused workload.

The helper performs these operations:

1. Validate the environment, wallet name, and configured hotkey file locally.
2. Register or reuse the public SSH key.
3. Create or reuse a persistent volume and mount it at `/data`.
4. Deploy the selected public Claims image in `idle` mode.
5. Stream the environment and wallet over SSH with restrictive permissions.
6. For a miner, configure its direct Axon address using the assigned public IP.
7. Verify the copied hotkey and update the workload arguments to either
   `validator --logging.debug` or `miner --logging.debug`.

If a step fails, the workload and volume are left in place for inspection. The
helper refuses to replace wallet files on an existing volume unless
`--replace-existing` is explicitly supplied.

Add `--leave-idle` to prepare the rental, persistent volume, environment, and
wallet without starting the miner or validator. The workload remains in idle
mode and continues to be billable. To start it later as a persistent service,
edit its Targon arguments to `validator`, `--logging.debug` or `miner`,
`--logging.debug`; Targon will redeploy it with that role as the container's
main process. You can also connect over SSH and run `claims-node validator
--logging.debug` or `claims-node miner --logging.debug` interactively.

### Manual Bootstrap

The same setup can be performed manually when API automation is not desired:

1. Create an empty persistent volume.
2. Create an idle rental from the appropriate Claims image, mount the volume at
   `/data`, and attach an SSH key.
3. Copy the private state into the mounted volume:

```bash
ssh -i ~/.ssh/id_ed25519 <WORKLOAD_UID>@ssh.deployments.targon.com \
  'mkdir -p /data/env /data/bittensor/wallets && chmod 700 /data/env /data/bittensor'

cat .validator.testnet.env | \
  ssh -i ~/.ssh/id_ed25519 <WORKLOAD_UID>@ssh.deployments.targon.com \
    'umask 077; cat > /data/env/validator.env'

tar -C ~/.bittensor/wallets -cf - claims-test-validator | \
  ssh -i ~/.ssh/id_ed25519 <WORKLOAD_UID>@ssh.deployments.targon.com \
    'tar -C /data/bittensor/wallets -xf - && chmod -R go-rwx /data/bittensor/wallets'
```

For a miner, substitute `.miner.testnet.env`, `/data/env/miner.env`, and the
miner wallet name. Configure a direct TCP port and set its public address in the
miner environment:

```env
BT_AXON_EXTERNAL_IP=<TARGON_DIRECT_PORT_PUBLIC_IP>
BT_AXON_PORT=8091
BT_AXON_EXTERNAL_PORT=8091
```

4. Verify the role-specific environment file and configured hotkey under
   `/data/bittensor/wallets/`.
5. Edit the rental arguments to `validator --logging.debug` or
   `miner --logging.debug`. Updating a running workload triggers a redeploy
   while preserving the mounted volume.

Do not put wallet JSON, mnemonics, or private keys into Targon environment
variables. Targon exposes environment variables as workload configuration;
SSH keeps the private material out of the API payload.

## Configure Targon Manually

1. Create one persistent volume for each node and mount it at `/data`.
2. Select the public miner or public validator image by its immutable SHA tag.
3. Add your SSH public key to the rental.
4. For a miner, expose its axon port as a direct TCP port, normally `8091`.
5. Set the runtime variables below through Targon or `/data/env/<role>.env`.

Common variables:

```env
BT_WALLET_NAME=claims-test-validator
BT_WALLET_HOTKEY=default
BT_NETUID=530
BT_SUBTENSOR_NETWORK=test
```

A miner also needs:

```env
BT_WALLET_NAME=claims-test-miner
BT_WALLET_HOTKEY=default
BT_AXON_EXTERNAL_IP=<TARGON_DIRECT_PORT_PUBLIC_IP>
BT_AXON_PORT=8091
BT_AXON_EXTERNAL_PORT=8091
```

Place restored wallets under `/data/bittensor/wallets/`. Provider credentials
can be supplied as Targon environment variables or written to the mode `0600`
file `/data/env/miner.env` or `/data/env/validator.env`. The image creates that
file from the matching public example on first boot. Container profiles should
set `CLAIMS_BRONZE_ROOT=/data/bronze` and
`CLAIMS_OUTPUT_DIR=/data/outputs/validator` so generated state survives rental
replacement.

## Start Modes

The default command is `idle`, which keeps the rental alive. SSH into it and
start the node manually:

```bash
claims-node miner --logging.debug
claims-node validator --logging.debug
```

For automatic startup, set the Targon workload argument to `miner` or
`validator`. If the dashboard requires an explicit command, use:

```text
Command: /usr/local/bin/claims-node
Arguments: validator --logging.debug
```

Use `miner --logging.debug` for a miner. SSH remains available while the neuron
is running because Targon provides SSH access to the rental container. For an
SSH-managed process that should survive disconnects, run it inside `tmux`.

## Updates And Rollback

Edit the workload image to a new immutable commit tag through Targon or `PATCH`
the [workload](https://docs.targon.com/api/workloads). Targon automatically
redeploys a running workload and remounts the same `/data` volume. The wallet,
environment, Hermes state, caches, Bronze data, and outputs remain in place.

Roll back by changing the workload image to the previous immutable tag. A new
template, volume, or wallet upload is not required. Create a replacement rental
only when the existing workload cannot be updated; attach the same volume when
doing so.
