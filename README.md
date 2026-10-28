# Claims

Claims is a Bittensor competition for extracting scientific claims and their supporting evidence from research papers. Miners submit an installable Python pipeline; Claims runs each selected package on the same released paper sample and scores its structured results.

## Enter the competition

1. Read the [current round rules](https://dashboard.claims111.ai/competition) for the submission deadline, permitted models, budgets, evaluation stages, and reward policy.
2. Build a Python wheel or source archive with an entrypoint that accepts `--request` and `--output` and writes an `agent_v1` artifact. The [sample miner package](https://github.com/DeSciClaims/claims-miner-package) provides a working extraction pipeline and manifest.
3. Submit the package through the [hotkey-signed upload service](https://artifacts.claims111.ai/docs). The package is stored privately, verified against its SHA-256, and bound to the submitting hotkey. Publish the [package commitment and manifest ID](./docs/submission.md) on Bittensor before the deadline.
4. After intake locks, Claims releases the randomly selected evaluation papers. The controller runs eligible packages, scores the outputs, completes Gold review, and publishes the results and weight vector. Follow the [competition dashboard](https://dashboard.claims111.ai/competition) for each round's progress and receipts.

Miner source is submitted with the package for private execution and review. Public round rules, sample, scores, and signed results make the evaluation inspectable without publishing competitors' code.

## How execution works

The controller retrieves the verified package and PDF, then sends a sealed one-paper job to a restricted worker over SSH. The worker installs the package in an isolated runtime and passes it a read-only PDF path, paper metadata, an inference endpoint, and a short-lived key for that paper. The pipeline may make multiple permitted model calls within the published budget. The controller validates the artifact, records token use, revokes the key, and stores the run log for retrieval by the submitting hotkey.

The OpenRouter credential and Bifrost administrative credential stay in trusted services. A miner process receives only its scoped gateway key. See [competition infrastructure](./docs/competition-infrastructure.md) for the controller and gateway configuration.

## Documentation

- [Sample miner package and local execution](https://github.com/DeSciClaims/claims-miner-package)
- [Package submission and commitment format](./docs/submission.md)
- [Extraction schema and neuron documentation](./docs/extraction.md)
- [Competition controller internals](./competition/README.md)
- [Public dashboard](https://dashboard.claims111.ai/competition)
- [Contribution guide](./CONTRIBUTING.md)
