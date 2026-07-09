# Implementation Roadmap

## Phase 1: Public Schema and Demo

- Freeze a lightweight public schema
- Share miner and validator examples
- Gather design feedback

## Phase 2: URL-Based Task Loop

- Add a task envelope whose primary input is a downloadable PDF URL
- Download and hash source PDFs before extraction
- Cache task artifacts by source URL, PDF content hash, parser version, and miner version
- Add more example papers and output variants
- Introduce small gold-set style examples

## Phase 3: Early Reference Implementations

- Publish a basic miner reference flow
- Publish a validator reference flow
- Start benchmarking extraction quality
- Add validator task providers for local manifests and remote paper queues

## Phase 4: Subnet Readiness

- Finalize scoring dimensions
- Finalize incentive design
- Prepare launch-ready documentation
- Harden public axon deployment, validator permit checks, protocol versioning,
  and weight-setting behavior

## Current Focus

The repository now includes runnable v0 miner, validator, and Bittensor neuron
entry points. Current work is focused on hardening the localnet loop, improving
validator scoring, and preparing the implementation for broader network testing.

## Hardening Plan

The production task entry point should be a downloadable PDF URL. Validators
should distribute tasks that identify the paper source, while miners should
download, verify, parse, cache, and extract from that source in a reproducible
way.

### PDF URL Task Source

The task envelope should include:

- `task_id`
- `paper_url`
- `paper_id`, when known
- `source_sha256`, when precomputed by the validator
- schema/protocol version

The validator can create these tasks from a local JSONL manifest, a remote queue,
or a benchmark set. The miner should not trust the URL blindly: it should enforce
download size limits, timeouts, content-type checks, and hash validation when a
hash is supplied.

The validator should not prescribe the PDF extraction method. Parser choice is
part of the miner implementation: a miner may use GROBID, pypdf, OCR, publisher
XML, or a hybrid parser. The validator should evaluate the submitted
claim-evidence output against the source document and reward the method that
produces the best grounded extraction.

### Artifact Creation

The current `artifact.json` should become a derived task artifact, not the
primary task input. A miner can build the artifact after downloading and parsing
the PDF. A validator can also prebuild artifacts for local smoke tests, but the
network-facing task should remain tied to the PDF URL and content hash.

### Miner Caching

Miner cache keys should include:

- normalized PDF URL
- PDF content hash
- parser implementation and version
- miner version
- prompt/model configuration

This prevents repeated LLM extraction for the same task and makes retry behavior
safe when validators query the same miner more than once.

### Validator Task Scheduling

The validator should move from a single `--claims.task-artifact` file to a task
provider interface:

- local artifact task provider for smoke tests
- local URL manifest provider for localnet and testnet
- remote URL queue provider for production operation

The single-artifact mode should remain available because it is useful for
debugging and deterministic local runs.

### Miner Selection

The validator should select miners from metagraph/subtensor data instead of a
manual UID scan. Eligible miners should be registered, have a serving axon, and
match the expected Claims protocol version.

### Public Axon Operation

Mainnet miners need a reachable axon with a public IP and open port:

```bash
python -m neurons.miner \
  --netuid <NETUID> \
  --wallet.name <MINER_WALLET> \
  --wallet.hotkey <HOTKEY> \
  --subtensor.network finney \
  --axon.port 8091 \
  --axon.external_ip <PUBLIC_IP> \
  --axon.external_port 8091
```

### Validator Weight Setting

Validators should preflight registration, stake, validator permit, commit-reveal
status, and weight timing before setting weights. An audit-only mode should let
operators score miners without submitting weights.

### Protocol Versioning

The synapse should carry an explicit protocol version and schema version so
validators can reject incompatible miners cleanly. Generic Bittensor miners will
not understand Claims tasks unless they implement the Claims protocol.
