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

## Configure Targon

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

To update, create a replacement rental from a new immutable image tag and mount
the same volume. Roll back by recreating the rental with the previous tag.
