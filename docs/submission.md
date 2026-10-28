# Package submission and commitment

Each round publishes its deadline block, allowed models, budgets, and other rules before intake closes. The submitting hotkey signs requests to the [miner upload API](https://artifacts.claims111.ai/docs).

1. Build the exact wheel or source archive you want evaluated, and record its SHA-256 and byte length.
2. Prepare a `claims_competition_manifest_v1` manifest with its real package filename, entrypoint, declared models, team ID, included source, and production-use licence. See the [sample manifest](https://github.com/DeSciClaims/claims-miner-package/blob/main/examples/competition_manifest.json).
3. Generate a random 32-byte salt. Send the manifest, package hash, size, salt, round ID, and network to `POST /miner/competition/uploads`. Upload the bytes to the returned private staging URL, then sign and send the same body to `POST /miner/competition/uploads/finalize`. The service verifies the bytes and stores a sealed receipt.
4. Publish a Bittensor commitment from the same hotkey before the deadline block. Its two fields are `[Sha256(h), Raw(manifest_id)]`, where `h = sha256(package_bytes || decoded_hotkey_public_key || salt32)` and `manifest_id = sha256(canonical_manifest_json).hexdigest()`. Canonical JSON uses sorted keys, compact separators, and UTF-8 without ASCII escaping. The raw manifest field contains the lowercase 64-character digest as ASCII bytes.

The controller reads the historical commitment at the deadline block and matches it to one sealed upload. Changing package bytes, manifest, hotkey, or salt changes the match. The implementation of the [commitment check](../competition/commitments.py) is the authoritative reference.
