#!/usr/bin/env python3
"""Create and bootstrap a Targon miner or validator rental.

The Targon API manages the workload and persistent volume. Private node
configuration and wallet files are streamed over SSH and never included in an
API request or workload environment variable.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


DEFAULT_API_BASE = "https://api.targon.com"
DEFAULT_SSH_HOST = "ssh.deployments.targon.com"


class BootstrapError(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalInputs:
    env_file: Path
    wallet_dir: Path
    wallet_name: str
    hotkey_name: str
    ssh_private_key: Path
    ssh_public_key: Path


class TargonApi:
    def __init__(self, *, base_url: str, token: str, org: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.org = org

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=60) as response:
                data = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise BootstrapError(
                f"Targon API {method} {path} failed with HTTP {exc.code}: {detail}"
            ) from exc
        except URLError as exc:
            raise BootstrapError(f"Targon API {method} {path} failed: {exc}") from exc
        if not data:
            return {}
        try:
            result = json.loads(data)
        except json.JSONDecodeError as exc:
            raise BootstrapError(
                f"Targon API {method} {path} returned invalid JSON"
            ) from exc
        if not isinstance(result, dict):
            raise BootstrapError(
                f"Targon API {method} {path} returned an unexpected response"
            )
        return result

    def org_path(self, suffix: str) -> str:
        return f"/tha/v3/orgs/{quote(self.org, safe='')}/{suffix.lstrip('/')}"


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value and value[0:1] == value[-1:] and value.startswith(("'", '"')):
            value = value[1:-1]
        values[key] = value
    return values


def validate_local_inputs(args: argparse.Namespace) -> LocalInputs:
    env_file = Path(args.env_file).expanduser().resolve()
    wallet_dir = Path(args.wallet_dir).expanduser().resolve()
    ssh_private_key = Path(args.ssh_private_key).expanduser().resolve()
    ssh_public_key = Path(args.ssh_public_key).expanduser().resolve()
    for label, path in (
        (f"{args.role} environment", env_file),
        ("wallet directory", wallet_dir),
        ("SSH private key", ssh_private_key),
        ("SSH public key", ssh_public_key),
    ):
        if not path.exists():
            raise BootstrapError(f"{label} does not exist: {path}")
    if not env_file.is_file():
        raise BootstrapError(f"{args.role} environment is not a file: {env_file}")
    if not wallet_dir.is_dir():
        raise BootstrapError(f"wallet directory is not a directory: {wallet_dir}")

    values = parse_env_file(env_file)
    wallet_name = values.get("BT_WALLET_NAME", "").strip()
    hotkey_name = values.get("BT_WALLET_HOTKEY", "").strip()
    if not wallet_name or not hotkey_name:
        raise BootstrapError(
            f"{args.role} environment must define BT_WALLET_NAME and BT_WALLET_HOTKEY"
        )
    if wallet_dir.name != wallet_name:
        raise BootstrapError(
            f"wallet directory name {wallet_dir.name!r} does not match "
            f"BT_WALLET_NAME={wallet_name!r}"
        )
    hotkey_file = wallet_dir / "hotkeys" / hotkey_name
    if not hotkey_file.is_file():
        raise BootstrapError(
            f"configured hotkey file does not exist: {hotkey_file}"
        )
    has_provider_key = any(
        values.get(name, "").strip()
        for name in ("OPENROUTER_API_KEY", "CHUTES_API_KEY")
    )
    if not has_provider_key and not args.allow_missing_provider_key:
        raise BootstrapError(
            f"{args.role} environment has no supported provider API key; use "
            "--allow-missing-provider-key only when another configured provider supplies credentials"
        )
    return LocalInputs(
        env_file=env_file,
        wallet_dir=wallet_dir,
        wallet_name=wallet_name,
        hotkey_name=hotkey_name,
        ssh_private_key=ssh_private_key,
        ssh_public_key=ssh_public_key,
    )


def build_workload_payload(
    args: argparse.Namespace,
    *,
    volume_uid: str,
    ssh_key_uid: str,
) -> dict[str, Any]:
    if args.role == "validator":
        envs = [
            {"name": "CLAIMS_BRONZE_ROOT", "value": "/data/bronze"},
            {
                "name": "CLAIMS_OUTPUT_DIR",
                "value": "/data/outputs/validator",
            },
        ]
        ports: list[dict[str, Any]] = []
    else:
        envs = [
            {
                "name": "CLAIMS_MINER_OUTPUT_DIR",
                "value": "/data/outputs/miner",
            }
        ]
        ports = [
            {
                "port": args.axon_port,
                "protocol": "TCP",
                "routing": "DIRECT",
            }
        ]
    return {
        "name": args.name,
        "image": args.image,
        "resource_name": args.resource_name,
        "type": "RENTAL",
        "args": ["idle"],
        "envs": envs,
        "ports": ports,
        "ssh_keys": [ssh_key_uid],
        "volumes": [
            {"uid": volume_uid, "mount_path": "/data", "read_only": False}
        ],
    }


def build_runtime_update(
    args: argparse.Namespace,
    *,
    state: dict[str, Any],
    workload_payload: dict[str, Any],
) -> dict[str, Any]:
    runtime_envs = list(workload_payload["envs"])
    if args.role == "miner":
        public_ip = str(state.get("public_ip", "")).strip()
        if not public_ip:
            raise BootstrapError(
                "Targon did not report a public IP for the miner's direct Axon port"
            )
        runtime_envs.extend(
            [
                {"name": "BT_AXON_EXTERNAL_IP", "value": public_ip},
                {"name": "BT_AXON_PORT", "value": str(args.axon_port)},
                {
                    "name": "BT_AXON_EXTERNAL_PORT",
                    "value": str(args.axon_port),
                },
            ]
        )
    return {
        "args": [args.role, "--logging.debug"],
        "envs": runtime_envs,
    }


def validate_reusable_workload(
    args: argparse.Namespace,
    workload: dict[str, Any],
) -> None:
    workload_type = str(workload.get("type") or "RENTAL").upper()
    if workload_type != "RENTAL":
        raise BootstrapError(
            f"existing workload {args.workload_uid} is {workload_type}, not RENTAL"
        )

    state = workload.get("state") if isinstance(workload.get("state"), dict) else {}
    status = str(state.get("status") or workload.get("status") or "").lower()
    if status not in {"registered", "suspended", "error"}:
        raise BootstrapError(
            f"existing workload {args.workload_uid} is {status or 'unknown'}; "
            "suspend it before reconfiguration"
        )

    resource = workload.get("resource") if isinstance(workload.get("resource"), dict) else {}
    resource_name = str(resource.get("name") or workload.get("resource_name") or "").strip()
    if resource_name != args.resource_name:
        raise BootstrapError(
            f"existing workload {args.workload_uid} uses resource "
            f"{resource_name or 'unknown'}, not {args.resource_name}"
        )


def build_idle_workload_update(workload_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": workload_payload["name"],
        "image": workload_payload["image"],
        "commands": [],
        "args": ["idle"],
        "envs": workload_payload["envs"],
        "ports": workload_payload["ports"],
        "ssh_keys": workload_payload["ssh_keys"],
        "volumes": workload_payload["volumes"],
    }


def attached_data_volume_uid(workload: dict[str, Any]) -> str:
    volumes = workload.get("volumes")
    if not isinstance(volumes, list):
        return ""
    matches = [
        str(volume.get("uid") or "").strip()
        for volume in volumes
        if isinstance(volume, dict) and volume.get("mount_path") == "/data"
    ]
    matches = [uid for uid in matches if uid]
    if len(matches) > 1:
        raise BootstrapError("existing workload has multiple volumes mounted at /data")
    return matches[0] if matches else ""


def ensure_ssh_key(api: TargonApi, inputs: LocalInputs, requested_uid: str) -> str:
    if requested_uid:
        return requested_uid
    public_key = inputs.ssh_public_key.read_text(encoding="utf-8").strip()
    if not public_key.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-")):
        raise BootstrapError(f"unsupported SSH public key: {inputs.ssh_public_key}")
    listed = api.request("GET", api.org_path("ssh-keys?limit=1000"))
    for item in listed.get("items", []):
        if isinstance(item, dict) and item.get("public_key_raw", "").strip() == public_key:
            uid = str(item.get("uid", "")).strip()
            if uid:
                print(f"Using existing Targon SSH key {uid}.")
                return uid
    created = api.request(
        "POST",
        api.org_path("ssh-keys"),
        {"name": argsafe_name(inputs.ssh_public_key.stem), "ssh_key": public_key},
    )
    uid = str(created.get("uid", "")).strip()
    if not uid:
        raise BootstrapError("Targon did not return an SSH key UID")
    print(f"Created Targon SSH key {uid}.")
    return uid


def argsafe_name(value: str) -> str:
    normalized = "".join(character.lower() if character.isalnum() else "-" for character in value)
    normalized = "-".join(part for part in normalized.split("-") if part)
    return (normalized or "claims-bootstrap-key")[:32]


def ensure_volume(api: TargonApi, args: argparse.Namespace) -> tuple[str, bool]:
    if args.volume_uid:
        return args.volume_uid, False
    created = api.request(
        "POST",
        api.org_path("volumes"),
        {
            "name": args.volume_name or f"{args.name}-data",
            "size_in_mb": args.volume_size_gb * 1024,
            "resource_name": args.volume_resource_name,
        },
    )
    uid = str(created.get("uid", "")).strip()
    if not uid:
        raise BootstrapError("Targon did not return a volume UID")
    print(f"Created persistent volume {uid}.")
    return uid, True


def wait_for_workload(
    api: TargonApi,
    workload_uid: str,
    *,
    timeout_seconds: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_status = ""
    while time.monotonic() < deadline:
        state = api.request("GET", api.org_path(f"workloads/{workload_uid}/state"))
        status = str(state.get("status", "")).lower()
        if status != last_status:
            print(f"Targon workload status: {status or 'unknown'}")
            last_status = status
        if status == "running":
            return state
        if status in {"error", "deleted"}:
            raise BootstrapError(
                f"Targon workload entered {status}: {state.get('message', '')}"
            )
        time.sleep(5)
    raise BootstrapError(
        f"Targon workload did not become ready within {timeout_seconds} seconds"
    )


def ssh_base(inputs: LocalInputs, args: argparse.Namespace) -> list[str]:
    return [
        "ssh",
        "-i",
        str(inputs.ssh_private_key),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=10",
        "-o",
        f"ServerAliveInterval={args.ssh_keepalive_seconds}",
    ]


def wait_for_ssh(
    inputs: LocalInputs,
    args: argparse.Namespace,
    workload_uid: str,
) -> str:
    destination = f"{workload_uid}@{args.ssh_host}"
    deadline = time.monotonic() + args.wait_timeout
    while time.monotonic() < deadline:
        completed = subprocess.run(
            [*ssh_base(inputs, args), destination, "true"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if completed.returncode == 0:
            print("SSH transport is ready.")
            return destination
        time.sleep(5)
    raise BootstrapError(
        f"SSH did not become ready within {args.wait_timeout} seconds: {destination}"
    )


def run_ssh(
    inputs: LocalInputs,
    args: argparse.Namespace,
    destination: str,
    command: str,
    *,
    stdin: Any = None,
) -> None:
    completed = subprocess.run(
        [*ssh_base(inputs, args), destination, command],
        stdin=stdin,
        check=False,
    )
    if completed.returncode != 0:
        raise BootstrapError(f"remote bootstrap command failed: {command}")


def upload_private_state(
    inputs: LocalInputs,
    args: argparse.Namespace,
    destination: str,
    *,
    new_volume: bool,
) -> None:
    env_path = f"/data/env/{args.role}.env"
    wallets_path = "/data/bittensor/wallets"
    wallet_path = f"{wallets_path}/{inputs.wallet_name}"
    run_ssh(
        inputs,
        args,
        destination,
        "umask 077; mkdir -p /data/env /data/bittensor/wallets",
    )
    if not new_volume and not args.replace_existing:
        completed = subprocess.run(
            [
                *ssh_base(inputs, args),
                destination,
                f"test ! -e {shlex.quote(wallet_path)}",
            ],
            check=False,
        )
        if completed.returncode != 0:
            raise BootstrapError(
                f"existing volume already contains {wallet_path}; "
                "refusing to overwrite it without --replace-existing"
            )

    env_tmp = f"{env_path}.bootstrap"
    with inputs.env_file.open("rb") as stream:
        run_ssh(
            inputs,
            args,
            destination,
            f"umask 077; cat > {shlex.quote(env_tmp)} && "
            f"chmod 600 {shlex.quote(env_tmp)} && mv {shlex.quote(env_tmp)} {shlex.quote(env_path)}",
            stdin=stream,
        )

    if args.replace_existing:
        run_ssh(
            inputs,
            args,
            destination,
            f"rm -rf -- {shlex.quote(wallet_path)}",
        )
    remote_extract = (
        "set -eu; "
        f"tmp=$(mktemp -d {shlex.quote(wallets_path + '/.claims-bootstrap.XXXXXX')}); "
        "trap 'rm -rf \"$tmp\"' EXIT; "
        "tar -C \"$tmp\" -xf -; "
        f"chmod -R go-rwx \"$tmp/{inputs.wallet_name}\"; "
        f"mv \"$tmp/{inputs.wallet_name}\" {shlex.quote(wallet_path)}"
    )
    tar_process = subprocess.Popen(
        [
            "tar",
            "-C",
            str(inputs.wallet_dir.parent),
            "-cf",
            "-",
            inputs.wallet_name,
        ],
        stdout=subprocess.PIPE,
    )
    assert tar_process.stdout is not None
    try:
        run_ssh(
            inputs,
            args,
            destination,
            remote_extract,
            stdin=tar_process.stdout,
        )
    finally:
        tar_process.stdout.close()
    tar_returncode = tar_process.wait()
    if tar_returncode != 0:
        raise BootstrapError(f"failed to archive the local {args.role} wallet")

    hotkey_path = f"{wallet_path}/hotkeys/{inputs.hotkey_name}"
    run_ssh(
        inputs,
        args,
        destination,
        f"test -s {shlex.quote(env_path)} && test -s {shlex.quote(hotkey_path)}",
    )
    print(f"{args.role.capitalize()} environment and wallet copied to the persistent volume.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create a Targon miner or validator rental, securely copy its environment "
            "and wallet over SSH, and optionally enable automatic node startup."
        )
    )
    parser.add_argument("--role", choices=("miner", "validator"), required=True)
    parser.add_argument("--org", default=os.getenv("TARGON_ORG", ""))
    parser.add_argument("--name", required=True, help="Targon workload name")
    parser.add_argument("--image", required=True, help="immutable public Claims image")
    parser.add_argument("--resource-name", required=True, help="Targon rental resource name")
    parser.add_argument("--env-file", required=True, help="local role-specific .env file")
    parser.add_argument("--wallet-dir", required=True, help="local Bittensor wallet directory")
    parser.add_argument("--ssh-private-key", required=True)
    parser.add_argument(
        "--ssh-public-key",
        help="defaults to SSH_PRIVATE_KEY.pub",
    )
    parser.add_argument("--ssh-key-uid", default="", help="reuse an existing Targon SSH key")
    parser.add_argument(
        "--workload-uid",
        default="",
        help="reuse a suspended or registered Targon rental instead of creating one",
    )
    parser.add_argument("--volume-uid", default="", help="reuse an existing empty volume")
    parser.add_argument("--volume-name", default="")
    parser.add_argument("--volume-size-gb", type=int, default=20)
    parser.add_argument("--volume-resource-name", default="storage-rentals")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--ssh-host", default=DEFAULT_SSH_HOST)
    parser.add_argument("--axon-port", type=int, default=8091, help="miner direct TCP port")
    parser.add_argument("--wait-timeout", type=int, default=900)
    parser.add_argument("--ssh-keepalive-seconds", type=int, default=30)
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="replace wallet and environment files on an existing volume",
    )
    parser.add_argument(
        "--leave-idle",
        action="store_true",
        help=(
            "leave the configured workload in idle mode instead of starting the "
            "miner or validator"
        ),
    )
    parser.add_argument(
        "--allow-missing-provider-key",
        action="store_true",
        help="allow a profile without OPENROUTER_API_KEY or CHUTES_API_KEY",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.org:
        parser.error("--org or TARGON_ORG is required")
    if args.volume_size_gb <= 0:
        parser.error("--volume-size-gb must be positive")
    if args.wait_timeout <= 0:
        parser.error("--wait-timeout must be positive")
    if not 1024 <= args.axon_port <= 65535:
        parser.error("--axon-port must be between 1024 and 65535")
    if not args.ssh_public_key:
        args.ssh_public_key = f"{args.ssh_private_key}.pub"
    token = os.getenv("TARGON_API_TOKEN") or os.getenv("TARGON_API_KEY")
    if not token:
        parser.error("TARGON_API_TOKEN or TARGON_API_KEY is required")

    try:
        inputs = validate_local_inputs(args)
        api = TargonApi(base_url=args.api_base, token=token, org=args.org)
        existing_workload: dict[str, Any] | None = None
        if args.workload_uid:
            existing_workload = api.request(
                "GET",
                api.org_path(f"workloads/{args.workload_uid}"),
            )
            validate_reusable_workload(args, existing_workload)
            if not args.volume_uid:
                args.volume_uid = attached_data_volume_uid(existing_workload)
                if args.volume_uid:
                    print(f"Reusing attached persistent volume {args.volume_uid}.")
        ssh_key_uid = ensure_ssh_key(api, inputs, args.ssh_key_uid)
        volume_uid, new_volume = ensure_volume(api, args)
        workload_payload = build_workload_payload(
            args,
            volume_uid=volume_uid,
            ssh_key_uid=ssh_key_uid,
        )
        if existing_workload is not None:
            workload_uid = args.workload_uid
            api.request(
                "PATCH",
                api.org_path(f"workloads/{workload_uid}"),
                build_idle_workload_update(workload_payload),
            )
            print(f"Reconfigured existing workload {workload_uid} in idle mode.")
        else:
            workload = api.request(
                "POST",
                api.org_path("workloads"),
                workload_payload,
            )
            workload_uid = str(workload.get("uid", "")).strip()
            if not workload_uid:
                raise BootstrapError("Targon did not return a workload UID")
            print(f"Registered idle workload {workload_uid}.")
        api.request(
            "POST",
            api.org_path(f"workloads/{workload_uid}/deploy"),
        )
        state = wait_for_workload(api, workload_uid, timeout_seconds=args.wait_timeout)
        destination = wait_for_ssh(inputs, args, workload_uid)
        upload_private_state(
            inputs,
            args,
            destination,
            new_volume=new_volume,
        )
        if not args.leave_idle:
            runtime_update = build_runtime_update(
                args,
                state=state,
                workload_payload=workload_payload,
            )
            if args.role == "miner":
                print(
                    f"Configured miner Axon at {state['public_ip']}:{args.axon_port}."
                )
            api.request(
                "PATCH",
                api.org_path(f"workloads/{workload_uid}"),
                runtime_update,
            )
            wait_for_workload(api, workload_uid, timeout_seconds=args.wait_timeout)
    except BootstrapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print()
    print(f"Targon {args.role} bootstrap complete.")
    print(f"Workload: {workload_uid}")
    print(f"Volume:   {volume_uid}")
    if args.leave_idle:
        print(
            "The workload remains idle; its environment and wallet are ready on "
            "the persistent volume."
        )
    else:
        print(f"The {args.role} is now the container's main process and starts automatically.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
