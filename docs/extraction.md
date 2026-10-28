# Extraction and neuron documentation

Claims represents a paper's scientific contribution as an `agent_v1` artifact: paper metadata, claims, evidence records, source references, and a trace of the extraction. Claims must point to passages in the supplied paper so the validator can check both grounding and coverage.

The original miner implementation is in [`miner/agent_v1`](../miner/agent_v1/). It ingests the PDF, creates source spans with stable IDs, generates the structured artifact, and validates its references and shape. The [sample competition package](https://github.com/DeSciClaims/claims-miner-package) packages that extraction contract behind the competition entrypoint.

## References

- [Miner implementation and configuration](../miner/agent_v1/README.md)
- [Agent artifact schema](./0014-agent-v1-miner-system.md)
- [Claim and evidence schema](./0003-schema.md)
- [Canonical miner design](./0011-agent-v1-canonical-miner.md)
- [Validator and scoring design](./0015-agent-v1-validator-system.md)
- [Neuron configuration](../neurons/README.md)

The runnable competition package follows the same artifact contract. See its README for the request JSON, PDF mount, inference environment, build command, and example manifest.
