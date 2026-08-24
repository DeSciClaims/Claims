# Targon Container Deployment

The Claims images keep software in an immutable image and runtime state on one
persistent volume mounted at `/data`. Replacing a Targon rental therefore does
not remove wallets, environment files, Hermes state, model caches, Bronze
artifacts, outputs, or logs.

## Images

| Target | Contents | Registry visibility |
|---|---|---|
| `miner` | Claims, Python dependencies, Hermes | Public is acceptable |
| `validator` | Miner image contents plus the private reference miner package | Private only |

Never add `.env`, wallets, API keys, Git credentials, or deploy keys to either
image. The validator's reference-repository deploy key is used by CI only to
build the private image and is not copied into it.

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

The `Container images` workflow publishes the miner image on relevant `main`
and tag changes. Run it manually with `publish_validator=true` to publish the
private validator image.

Configure these repository settings first:

| Type | Name | Value |
|---|---|---|
| Secret | `CLAIMS_REFERENCE_DEPLOY_KEY` | Read-only private deploy key for `claims-reference-miner` |
| Variable | `CLAIMS_REFERENCE_MINER_REF` | Pinned reference miner tag or commit |
| Variable | `HERMES_COMMIT` | Pinned Hermes commit |

Keep the validator GHCR package private. Make the miner package public if
community miners should pull it without registry credentials. Deploy immutable
SHA tags rather than `edge` for reproducible rentals and rollback.

## Configure Targon

1. Create one persistent volume for each node and mount it at `/data`.
2. Select the miner or private validator image by its immutable SHA tag.
3. Add the appropriate GHCR registry credentials when the image is private.
4. Add your SSH public key to the rental.
5. For a miner, expose its axon port as a direct TCP port, normally `8091`.
6. Set the runtime variables below through Targon or `/data/env/<role>.env`.

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
file from the matching public example on first boot.

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

