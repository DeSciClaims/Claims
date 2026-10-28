# Competition worker and gateway configuration

The public repository contains transport code and configuration names. The controller host supplies values at runtime; SSH identities, known-host entries, Bifrost administration credentials, and upstream provider keys do not belong in Git or round rules.

## Controller to worker

The trusted worker coordinator reads these settings from its private environment. It is a separate systemd service from the round controller:

| Setting | Purpose |
| --- | --- |
| `CLAIMS_WORKER_VM_MODE=persistent` | Reuse a prepared worker VM. |
| `CLAIMS_WORKER_VM_UID`, `CLAIMS_WORKER_PUBLIC_IP`, `CLAIMS_WORKER_SSH_PORT`, `CLAIMS_WORKER_HOURLY_USD` | Identify that VM and account for its runtime. |
| `CLAIMS_WORKER_SSH_IDENTITY` | Path to the controller's private SSH identity, readable only by its service account. |
| `CLAIMS_WORKER_SSH_KNOWN_HOSTS` | Path to the worker host key verified and pinned by an operator. |
| `CLAIMS_WORKER_GATEWAY_IP` | Gateway address allowed by the worker firewall. |

For a systemd-managed worker coordinator, put the values in a controller-host file outside the Git checkout, for example `/etc/claims/competition-worker.env`:

```ini
CLAIMS_WORKER_VM_MODE=persistent
CLAIMS_WORKER_VM_UID=<worker-vm-id>
CLAIMS_WORKER_PUBLIC_IP=<worker-address>
CLAIMS_WORKER_SSH_PORT=<worker-ssh-port>
CLAIMS_WORKER_HOURLY_USD=<hourly-price>
CLAIMS_WORKER_SSH_IDENTITY=/home/claims-deploy/.ssh/competition-worker
CLAIMS_WORKER_SSH_KNOWN_HOSTS=/home/claims-deploy/.ssh/competition-worker-known_hosts
CLAIMS_WORKER_GATEWAY_IP=<gateway-address>
```

The `claims-competition-worker.service` unit loads it with `EnvironmentFile=/etc/claims/competition-worker.env`. The infra installer decrypts the SOPS file on the controller VM, installs the SSH identity for `claims-deploy`, and pins the verified worker host key. The environment file contains their paths, not their contents. The worker coordinator reads these variables when it starts. The separate `competition-controller.env` holds the round controller's signer and judge credentials.

For a worker fleet, the controller host runs `claims-competition-worker@<id>`
once per prepared VM. Each instance has its own root-only
`competition-worker-<id>.env` with that VM's address and cost; the shared
known-hosts file pins every VM key. Each VM executes one paper job at a time,
while the backend leases different jobs to the coordinators concurrently.

The SSH transport enforces strict host-key checking and uses a dedicated `claims-control` account. That account can invoke only the root-owned `claims-worker-control run` helper through sudo. The helper verifies the job bundle, enforces the configured gateway address and egress rules, runs the miner as the unprivileged `claims-miner` user, and returns bounded result and log data. The coordinator checks package and PDF hashes before dispatch and validates the returned artifact before recording the job.

## Inference access

`CLAIMS_BIFROST_MANAGEMENT_URL` and either `CLAIMS_BIFROST_ADMIN_BEARER` or `CLAIMS_BIFROST_ADMIN_USERNAME` plus `CLAIMS_BIFROST_ADMIN_PASSWORD` are available only to the trusted coordinator. Bifrost holds the upstream OpenRouter credential on the gateway host. The round's frozen `worker_policy.inference_base_url` identifies its HTTPS `/v1` endpoint and `worker_policy.allowed_models` sets the whitelist.

For each paper attempt, the coordinator creates a Bifrost virtual key restricted to that attempt's declared models, token budget, dollar budget, and expiry. It passes the key to the worker over the pinned SSH connection. The harness exposes `CLAIMS_INFERENCE_BASE_URL` and `CLAIMS_INFERENCE_TOKEN` only while the miner process runs. The miner can read this scoped key, so its limits and the worker's network restrictions are the protection against misuse. The controller checks Bifrost usage, revokes the key, redacts it from logs, and stores the logs in private object storage. The upstream OpenRouter key and Bifrost administration credential are never passed to the miner.

The coordinator's Hippius credentials and backend service credential likewise stay on the trusted host. Miners upload packages and retrieve their own logs through hotkey-signed upload-service endpoints, without bucket credentials.
