#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: claims-node MODE [neuron options]

Modes:
  idle       Keep the rental alive for SSH-first operation (default)
  miner      Start the Claims miner
  validator  Start the Claims validator
  shell      Open an interactive shell
  help       Show this help

Persistent state is stored under CLAIMS_DATA_ROOT (default: /data).
EOF
}

is_true() {
  case "${1:-}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

require_value() {
  local name="$1"
  local value="$2"
  if [[ -z "${value}" ]]; then
    echo "Required setting is missing: ${name}" >&2
    exit 2
  fi
}

role="${CLAIMS_NODE_ROLE:-}"
mode="${1:-idle}"
if [[ "${mode}" == "-h" || "${mode}" == "--help" || "${mode}" == "help" ]]; then
  usage
  exit 0
fi
shift || true

data_root="${CLAIMS_DATA_ROOT:-/data}"
mkdir -p \
  "${data_root}/bittensor" \
  "${data_root}/bronze" \
  "${data_root}/cache/dspy" \
  "${data_root}/cache/huggingface" \
  "${data_root}/cache/xdg" \
  "${data_root}/env" \
  "${data_root}/hermes" \
  "${data_root}/logs" \
  "${data_root}/outputs/miner" \
  "${data_root}/outputs/validator"
chmod 0700 "${data_root}/bittensor" "${data_root}/env" "${data_root}/hermes"

if [[ ! -e "${data_root}/hermes/.claims-image-seeded" ]]; then
  if [[ -d /opt/hermes-seed ]]; then
    cp -a -n /opt/hermes-seed/. "${data_root}/hermes/"
  fi
  touch "${data_root}/hermes/.claims-image-seeded"
fi

mkdir -p /root
if [[ ! -e /root/.bittensor ]]; then
  ln -s "${data_root}/bittensor" /root/.bittensor
fi

if [[ -n "${role}" ]]; then
  env_file="${CLAIMS_ENV_FILE:-${data_root}/env/${role}.env}"
  if [[ ! -f "${env_file}" ]]; then
    install -m 0600 "/opt/claims/examples/${role}.env.example" "${env_file}"
    echo "Created ${env_file}; add provider credentials before starting ${role}."
  fi
  ln -sfn "${env_file}" /opt/claims/.env
else
  env_file="${CLAIMS_ENV_FILE:-}"
fi

# Load host-local settings without evaluating the file as shell code. Explicit
# container environment variables take precedence over values in the env file.
if [[ -n "${env_file}" && -f "${env_file}" ]]; then
  while IFS= read -r -d '' setting; do
    key="${setting%%=*}"
    value="${setting#*=}"
    if [[ "${key}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ && -z "${!key+x}" ]]; then
      printf -v "${key}" '%s' "${value}"
      export "${key}"
    fi
  done < <(
    python - "${env_file}" <<'PY'
import os
import sys

from dotenv import dotenv_values

for key, value in dotenv_values(sys.argv[1]).items():
    if key and value is not None:
        os.write(1, f"{key}={value}".encode() + b"\0")
PY
  )
fi

export CLAIMS_DATA_ROOT="${data_root}"
export HERMES_HOME="${data_root}/hermes"
export HF_HOME="${HF_HOME:-${data_root}/cache/huggingface}"
export DSPY_CACHEDIR="${DSPY_CACHEDIR:-${data_root}/cache/dspy}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-${data_root}/cache/xdg}"
export CLAIMS_OUTPUT_DIR="${CLAIMS_OUTPUT_DIR:-${data_root}/outputs/validator}"
export CLAIMS_BRONZE_ROOT="${CLAIMS_BRONZE_ROOT:-${data_root}/bronze}"

hermes_provider="${HERMES_PROVIDER:-}"
hermes_model="${HERMES_MODEL:-}"
hermes_base_url="${HERMES_BASE_URL:-}"
miner_harness="${CLAIMS_AGENT_HARNESS:-}"
miner_model="${CLAIMS_AGENT_MODEL:-}"
if [[ -n "${env_file}" && -f "${env_file}" ]]; then
  mapfile -t image_settings < <(
    python - "${env_file}" "${role}" <<'PY'
import os
import sys

from dotenv import dotenv_values

values = {key: str(value or "").strip() for key, value in dotenv_values(sys.argv[1]).items()}
role = sys.argv[2]

def setting(name: str, fallback: str = "") -> str:
    return str(os.environ.get(name) or values.get(name) or fallback).strip()

provider = setting("HERMES_PROVIDER")
model = setting("HERMES_MODEL")
base_url = setting("HERMES_BASE_URL")
if role == "validator":
    provider = provider or setting("CLAIMS_RIGOR_PROVIDER") or setting("CLAIMS_SILVER_FILE_AGENT_PROVIDER")
    model = model or setting("CLAIMS_RIGOR_MODEL") or setting("CLAIMS_SILVER_FILE_AGENT_COMPARISON_MODEL")
    base_url = base_url or setting("CLAIMS_RIGOR_API_BASE")
else:
    provider = provider or setting("SUBNET_CLAIMS_AGENT_PROVIDER")
    model = model or setting("SUBNET_CLAIMS_AGENT_MODEL") or setting("CLAIMS_AGENT_MODEL")

provider = provider or "openrouter"
model = model or "deepseek/deepseek-v4-flash"
if not base_url and provider == "openrouter":
    base_url = setting("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1")
if not base_url and provider == "chutes":
    base_url = setting("CHUTES_API_BASE", "https://llm.chutes.ai/v1")

print(provider)
print(model)
print(base_url)
print(setting("CLAIMS_AGENT_HARNESS", "hermes-cli"))
print(setting("CLAIMS_AGENT_MODEL", model))
PY
  )
  hermes_provider="${hermes_provider:-${image_settings[0]:-openrouter}}"
  hermes_model="${hermes_model:-${image_settings[1]:-deepseek/deepseek-v4-flash}}"
  hermes_base_url="${hermes_base_url:-${image_settings[2]:-}}"
  miner_harness="${miner_harness:-${image_settings[3]:-hermes-cli}}"
  miner_model="${miner_model:-${image_settings[4]:-${hermes_model}}}"
fi

if ! is_true "${CLAIMS_SKIP_HERMES_CONFIG:-false}"; then
  if [[ "${hermes_provider}" == "chutes" ]]; then
    hermes config set providers.chutes.name "Chutes" >/dev/null
    hermes config set providers.chutes.base_url "${hermes_base_url:-https://llm.chutes.ai/v1}" >/dev/null
    hermes config set providers.chutes.key_env "CHUTES_API_KEY" >/dev/null
    hermes config set providers.chutes.transport "openai_chat" >/dev/null
  fi
  hermes config set model.provider "${hermes_provider:-openrouter}" >/dev/null
  hermes config set model.default "${hermes_model:-deepseek/deepseek-v4-flash}" >/dev/null
  if [[ -n "${hermes_base_url}" ]]; then
    hermes config set model.base_url "${hermes_base_url}" >/dev/null
  fi
fi

wallet_name="${BT_WALLET_NAME:-}"
wallet_hotkey="${BT_WALLET_HOTKEY:-}"
wallet_path="${BT_WALLET_PATH:-${data_root}/bittensor/wallets}"
netuid="${BT_NETUID:-530}"
subtensor_network="${BT_SUBTENSOR_NETWORK:-test}"

cd /opt/claims
case "${mode}" in
  idle)
    echo "Claims ${role:-node} image ready. SSH in and run: claims-node ${role:-miner} --logging.debug"
    exec sleep infinity
    ;;
  shell)
    exec "${SHELL:-/bin/bash}" "$@"
    ;;
  miner)
    if [[ -n "${role}" && "${role}" != "miner" ]]; then
      echo "This is a ${role} image; refusing to start miner mode." >&2
      exit 2
    fi
    require_value BT_WALLET_NAME "${wallet_name}"
    require_value BT_WALLET_HOTKEY "${wallet_hotkey}"
    external_ip="${BT_AXON_EXTERNAL_IP:-}"
    axon_port="${BT_AXON_PORT:-8091}"
    require_value BT_AXON_EXTERNAL_IP "${external_ip}"
    exec python -m neurons.miner \
      --netuid "${netuid}" \
      --wallet.name "${wallet_name}" \
      --wallet.hotkey "${wallet_hotkey}" \
      --wallet.path "${wallet_path}" \
      --subtensor.network "${subtensor_network}" \
      --axon.ip "${BT_AXON_IP:-0.0.0.0}" \
      --axon.external_ip "${external_ip}" \
      --axon.port "${axon_port}" \
      --axon.external_port "${BT_AXON_EXTERNAL_PORT:-${axon_port}}" \
      --claims.pipeline "${CLAIMS_MINER_PIPELINE:-agent_v1}" \
      --claims.agent-harness "${miner_harness:-hermes-cli}" \
      --claims.agent-model "${miner_model:-deepseek/deepseek-v4-flash}" \
      --claims.pdf-extraction-method "${SUBNET_CLAIMS_PDF_READER:-pdf-inspector}" \
      --claims.batch-max-workers "${CLAIMS_MINER_BATCH_MAX_WORKERS:-2}" \
      --claims.output-dir "${CLAIMS_MINER_OUTPUT_DIR:-${data_root}/outputs/miner}" \
      "$@"
    ;;
  validator)
    if [[ -n "${role}" && "${role}" != "validator" ]]; then
      echo "This is a ${role} image; refusing to start validator mode." >&2
      exit 2
    fi
    require_value BT_WALLET_NAME "${wallet_name}"
    require_value BT_WALLET_HOTKEY "${wallet_hotkey}"
    exec python -m neurons.validator \
      --netuid "${netuid}" \
      --wallet.name "${wallet_name}" \
      --wallet.hotkey "${wallet_hotkey}" \
      --wallet.path "${wallet_path}" \
      --subtensor.network "${subtensor_network}" \
      "$@"
    ;;
  *)
    exec "${mode}" "$@"
    ;;
esac
