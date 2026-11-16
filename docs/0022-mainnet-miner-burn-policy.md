# Mainnet miner weight burn policy

Effective for the validator's new weight submissions from 12 November 2026.

The validator directs **87.5758%** of its weight vector to subnet owner UID 0, which is configured for burn. The remaining **12.4242%** is allocated to miner hotkeys. This changes weights, not Silver scores or the subnet's owner/validator/miner emission split.

Until the first weekly competition result is published, the validator uses the last signed extraction allocation as its carry-forward. It removes the former operator share, preserves the relative shares of the miner recipients, and scales those shares into the 12.4242% miner pool. A miner hotkey that is no longer registered burns its share.

For each new signed weekly competition result, production, runner-up 1 and runner-up 2 receive **70% / 20% / 10% of the 12.4242% miner pool**, respectively. An unfilled or ineligible slot is burned in addition to the fixed 87.5758%. The signed result names the allocation policy `burn_875758_miner_70_20_10_v1` and publishes its full weight vector.

The validator's first carry-forward transaction under this policy was accepted at block **9,486,540**. Its encrypted weight commitment takes effect when the chain reveals it. Previously accepted weights and historical result receipts are unchanged.

The proposed change to subnet emission shares requires a separate owner multisig action. If the miner share becomes 33%, allocating 12.4242% of that share to miners is approximately 4.1% of the subnet's pre-burn emission. Actual earnings also depend on the chain's burn adjustment to subnet emissions and other validators' weights; this calculation is not a payment guarantee.
