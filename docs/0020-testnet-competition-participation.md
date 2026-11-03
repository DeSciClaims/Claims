# Participate in daily testnet competitions

Build a Python extraction package, submit it with a registered testnet hotkey, and commit its identity on-chain. Claims runs it on papers released after the submission deadline. You do not need to operate an extraction server or provide an OpenRouter key.

## Network and schedule

| Setting | Current testnet value |
| --- | --- |
| Bittensor network | Testnet, netuid **530** |
| Chain RPC | `wss://test.finney.opentensor.ai:443` |
| Public round API | `https://api.claims111.ai` |
| Signed uploads and logs | `https://artifacts.claims111.ai` |
| Dashboard | [Testnet competitions](https://dashboard.claims111.ai/competition?network=testnet) |
| Daily submission deadline | **12:00 UTC (noon)** |
| Evaluation stages | Prescreen **2** papers → screen **5** → final **10** |

Testnet competitions use the production Claims services. Select `network=testnet` in API queries and signed requests, and use testnet subnet 530 for chain operations.

These are the defaults as of 3 November 2026. Each round's published rules control its actual deadlines, models, budgets and stages. A new round opens only after the previous round has published and completed its production handoff; missed intake windows can be skipped. Evaluation continues after intake closes.

## 1. Register your hotkey

Create or reuse a Bittensor wallet, fund its coldkey with test TAO, and register the hotkey on subnet 530:

```sh
btcli subnet register --network test --netuid 530 \
  --wallet-name YOUR_WALLET --hotkey YOUR_HOTKEY
```

Review the registration cost before confirming. Mainnet registration does not register a hotkey on testnet.

Prior consensus reviews are currently **waived for testnet package entry**. Registration, signatures, commitments, duplicate detection and team/funding checks still apply. Declare the same team ID for entries you control; different IDs do not establish independence. The backend resolves linked teams and applies advancement limits. See [team identity](./submission.md#team-identity).

## 2. Find an open round

```sh
curl -fsS 'https://api.claims111.ai/public/competition/rounds?network=testnet&limit=25'
```

Choose a record with `status: "open"`. Copy its `round_id`, `deadline_at` and `deadline_block`. If none is open, wait for the next intake. Do not submit to an evaluating or published round.

```sh
export ROUND_ID='COPY_THE_OPEN_ROUND_ID'
curl -fsS "https://api.claims111.ai/public/competition/rounds/$ROUND_ID/rules"
curl -fsS "https://api.claims111.ai/public/competition/rounds/$ROUND_ID/entry-policy"
```

Finalize your upload before `deadline_at`, and have the chain commitment **included by `deadline_block`**. Broadcasting at the last moment is insufficient. Papers are released after this cutoff; miners have no subsequent manual code-reveal step.

## 3. Build your package

Start from the [sample extraction package](https://github.com/DeSciClaims/claims-miner-package), which includes build instructions, a request example and a manifest. Submit a wheel containing source, or a supported `tar.gz` source archive. Your declared `module:function` entrypoint receives:

```text
--request /path/to/request.json --output /path/to/output.json
```

It must write one `agent_v1` JSON artifact for the supplied paper. You may use your own PDF extractor and make multiple permitted model calls. Follow the existing [artifact and source-reference schema](./0014-agent-v1-miner-system.md); ground claims and evidence in the supplied PDF.

The current worker runs **Python 3.12.3, Linux x86_64**, with an offline wheelhouse. It includes the sample package's `pydantic`, `pypdf` and `fonttools` dependencies. Additional dependencies must be available offline and compatible with the worker. The job cannot download packages, call arbitrary internet services or access storage directly.

Update your manifest's team ID, entrypoint, real package filename, model IDs, source declaration and production-use licence declaration. The current extraction whitelist is:

- `deepseek/deepseek-v4-flash-0731`
- `qwen/qwen3-32b`

Current extraction limits are **$0.10 per package per paper**, an **800,000 input-plus-output token ceiling**, and **8,192 maximum output tokens per call**. The effective challenger token allowance is 1.5× baseline usage, bounded by that ceiling; the ceiling is used when usable baseline usage is unavailable. These limits cover package inference, separately from validator judging.

Read the actual allowance from `request.inference.budget`: `token_limit`, `dollar_limit_usd`, `remaining_tokens` and `remaining_dollar_limit_usd`. Remaining amounts are a snapshot at attempt start; retries share the original paper budget. The worker supplies `CLAIMS_INFERENCE_BASE_URL` and a paper-scoped `CLAIMS_INFERENCE_TOKEN`. Never log the token. The sample handles Bifrost's `openrouter/` wire-model prefix; keep plain catalog IDs in the manifest. See [inference details](./submission.md#worker-inference).

## 4. Upload and seal the package

Run the following examples from a checkout of this repository. Install its Python dependencies first:

```sh
git clone https://github.com/DeSciClaims/Claims.git
cd Claims
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .

export WALLET_NAME='YOUR_WALLET'
export HOTKEY_NAME='YOUR_HOTKEY'
export PACKAGE='/absolute/path/to/dist/your_package-0.1.0-py3-none-any.whl'
export MANIFEST='/absolute/path/to/competition_manifest.json'
# Keep ROUND_ID set to the open round selected above.
```

This uses the existing hotkey-signed client. No bucket account or storage credentials are needed. It saves the salt and sealed receipt locally so the same package identity can be checked on-chain.

```sh
.venv/bin/python - <<'PY'
import hashlib, json, os, secrets
from pathlib import Path
import requests
from bittensor_wallet import Wallet
from neurons.backend_client import ClaimsBackendClient

wallet = Wallet(name=os.environ["WALLET_NAME"], hotkey=os.environ["HOTKEY_NAME"])
round_id = os.environ["ROUND_ID"]
package = Path(os.environ["PACKAGE"]).read_bytes()
manifest = json.loads(Path(os.environ["MANIFEST"]).read_text())
if manifest["package_filename"] != Path(os.environ["PACKAGE"]).name:
    raise SystemExit("Manifest filename must match the actual package filename")
folder = Path.home() / ".local/share/claims/testnet"
folder.mkdir(parents=True, exist_ok=True, mode=0o700)
state_path = folder / f"{round_id}-{wallet.hotkey.ss58_address}.json"
payload = {
    "round_id": round_id, "network": "testnet",
    "package_sha256": hashlib.sha256(package).hexdigest(),
    "package_size_bytes": len(package), "manifest": manifest,
}
if state_path.exists():
    state = json.loads(state_path.read_text())
    if any(state["payload"][key] != value for key, value in payload.items()):
        raise SystemExit("Saved submission differs; use its original package and manifest")
else:
    payload["salt_hex"] = secrets.token_hex(32)
    state = {"payload": payload}
    with os.fdopen(os.open(state_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
        json.dump(state, f)
payload = state["payload"]
client = ClaimsBackendClient(
    base_url="https://artifacts.claims111.ai", wallet=wallet,
    network="testnet", timeout_seconds=90, max_retries=0,
)
if "receipt" not in state:
    offer = client.post("/miner/competition/uploads", payload)
    response = requests.put(offer["upload_url"], data=package,
                            headers=offer["required_headers"], timeout=90)
    response.raise_for_status()
    state["receipt"] = client.post("/miner/competition/uploads/finalize", payload)
    state_path.write_text(json.dumps(state, indent=2))
print("Sealed submission saved:", state_path)
PY
```

Keep this state file and the exact package bytes. An upload alone is not an entry: publish the matching commitment next. If an upload request times out, preserve the saved salt and resolve its status before changing package identity.

## 5. Commit from the same hotkey

The competition uses two chain fields, `Sha256` and `Raw64`. A generic text-only commitment is insufficient. This example refuses nonzero transaction fees or deposits; review any changed chain charges before adapting it.

```sh
.venv/bin/python - <<'PY'
import hashlib, json, os
from datetime import datetime, timezone
from pathlib import Path
import requests
from bittensor_wallet import Wallet
from async_substrate_interface.sync_substrate import SubstrateInterface
from competition.commitments import decode_fields, manifest_id
from competition.policy import commitment_hash

wallet = Wallet(name=os.environ["WALLET_NAME"], hotkey=os.environ["HOTKEY_NAME"])
round_id = os.environ["ROUND_ID"]
state_path = Path.home() / ".local/share/claims/testnet" / f"{round_id}-{wallet.hotkey.ss58_address}.json"
state = json.loads(state_path.read_text())
if "receipt" not in state:
    raise SystemExit("Finalize the upload first")
payload = state["payload"]
package = Path(os.environ["PACKAGE"]).read_bytes()
if hashlib.sha256(package).hexdigest() != payload["package_sha256"]:
    raise SystemExit("Package bytes differ from the sealed upload")
response = requests.get(f"https://api.claims111.ai/public/competition/rounds/{round_id}/rules", timeout=30)
response.raise_for_status()
rules = response.json()
digest = commitment_hash(package, wallet.hotkey.public_key, bytes.fromhex(payload["salt_hex"]))
mid = manifest_id(payload["manifest"])
chain = SubstrateInterface("wss://test.finney.opentensor.ai:443", max_retries=1)
try:
    assert chain.get_block_hash(0) == "0x8f9cf856bf558a14440e75569c9e58594757048d7b3a84b5d25f6bd978263105"
    record = chain.query("Commitments", "CommitmentOf", [530, wallet.hotkey.ss58_address]).value
    matches = False
    if record:
        try:
            h, m, block = decode_fields(record)
            matches = h.hex() == digest and m == mid and block <= rules["deadline_block"]
        except ValueError:
            pass
    if not matches:
        if "pending_extrinsic" in state:
            raise SystemExit("An earlier broadcast is unresolved; reconcile it before resubmitting")
        assert datetime.now(timezone.utc) < datetime.fromisoformat(rules["deadline_at"])
        assert chain.get_block_number(chain.get_chain_head()) < rules["deadline_block"]
        for name in ("InitialDeposit", "FieldDeposit"):
            assert int(chain.get_constant("Commitments", name).value) == 0, "Review changed deposits"
        call = chain.compose_call("Commitments", "set_commitment", {
            "netuid": 530,
            "info": {"fields": [[{"Sha256": list(bytes.fromhex(digest))},
                                  {"Raw64": list(mid.encode("ascii"))}]]},
        })
        assert int(chain.get_payment_info(call, wallet.hotkey)["partial_fee"]) == 0, "Review changed fee"
        extrinsic = chain.create_signed_extrinsic(call=call, keypair=wallet.hotkey)
        state["pending_extrinsic"] = str(extrinsic.extrinsic_hash)
        state_path.write_text(json.dumps(state, indent=2))
        receipt = chain.submit_extrinsic(extrinsic, wait_for_inclusion=True, wait_for_finalization=True)
        assert receipt.is_success, receipt.error_message
        record = chain.query("Commitments", "CommitmentOf", [530, wallet.hotkey.ss58_address]).value
        h, m, block = decode_fields(record)
        assert h.hex() == digest and m == mid and block <= rules["deadline_block"]
    state["commit_block"] = block
    state.pop("pending_extrinsic", None)
    state_path.write_text(json.dumps(state, indent=2))
    print("Matching commitment verified at block", block)
finally:
    chain.close()
PY
```

Submit early enough for finalization. If a broadcast times out, inspect the saved transaction and on-chain commitment before retrying. Do not generate a new salt to recover an uncertain transaction. Avoid overwriting this hotkey's commitment until intake locks. The [submission specification](./submission.md) explains how Claims matches the historical chain record to the sealed upload.

## 6. Follow evaluation and retrieve logs

The controller handles paper release, baseline extraction, package execution, judging, promotion and publication. The standard screen permits at most two entries per resolved team, and final permits one. A first round without an incumbent can instead use the published launch-stage plan. Final ranking and replacement rules are in the frozen rules; consensus reviews do not determine this competition's winner.

Public progress, scores and signed results:

```sh
curl -fsS "https://api.claims111.ai/public/competition/rounds/$ROUND_ID/progress"
curl -fsS "https://api.claims111.ai/public/competition/rounds/$ROUND_ID/score-summary"
curl -fsS "https://api.claims111.ai/public/competition/rounds/$ROUND_ID/results"
```

Results are available after publication. Follow the [testnet competition dashboard](https://dashboard.claims111.ai/competition?network=testnet), or open `https://dashboard.claims111.ai/competition/ROUND_ID` for a specific round. To run the [dashboard locally](https://github.com/DeSciClaims/Claims-Dashboard), set `NEXT_PUBLIC_CLAIMS_API_URL=https://api.claims111.ai` in its `.env.local`, run it, and open `http://localhost:3000/competition/ROUND_ID`.

Retrieve your own installation and execution logs using the same hotkey:

```sh
.venv/bin/python - <<'PY'
import os
from bittensor_wallet import Wallet
from neurons.backend_client import ClaimsBackendClient

client = ClaimsBackendClient(
    base_url="https://artifacts.claims111.ai", network="testnet",
    wallet=Wallet(name=os.environ["WALLET_NAME"], hotkey=os.environ["HOTKEY_NAME"]),
)
query = {}
while True:
    page = client.get(f"/miner/competition/rounds/{os.environ['ROUND_ID']}/logs", query=query)
    for log in page["logs"]:
        print(log["object_key"], log["download_url"])
    token = page.get("continuation_token")
    if not token:
        break
    query = {"continuation_token": token}
PY
```

Each log belongs to one package–paper execution attempt, including failed attempts, and is uploaded after that attempt finishes. Download URLs expire after **10 minutes**; request fresh links when needed. Keep them private. A new daily round requires a fresh round-specific upload, salt and commitment, even if you reuse the same package.
