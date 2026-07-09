# Incentive Mechanism

This document records the current incentive-design direction. The runnable v0
loop uses validator scores to set weights, while the full incentive policy will
continue to evolve with benchmark and network results.

## Design Goal

The subnet should reward miners for producing claim-evidence records that are:

- accurate
- grounded
- sufficiently granular
- useful to downstream validators and customers

Validators should be rewarded for:

- selecting valuable tasks
- maintaining high-quality evaluation standards
- curating reusable canonical graph outputs

## Open Questions

- How much weight should gold-set performance carry?
- How should validator disagreement be handled?
- How do we discourage shallow copying or consensus gaming?
- How should downstream utility feed back into on-chain incentives?

The current implementation makes the data product and scoring loop concrete so
these policy questions can be tested against real miner outputs.
