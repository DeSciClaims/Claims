#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: install-node.sh ROLE [options]

ROLE is miner or validator.

Options:
  --python COMMAND               Python command (default: python3)
  --recreate-venv               Clear and rebuild an existing .venv
  --env-file FILE               Runtime environment file (default: .env)
  --skip-system-packages        Do not install Ubuntu packages
  --skip-hermes                 Do not install Hermes Agent
  --reference-repo-url URL      Public reference repository URL (validator only)
  --reference-repo-version REF  Reference branch, tag, or commit (default: main)
  --reference-dir DIR           Reference checkout (default: ../claims-reference-miner)
  -h, --help                    Show this help

Run this script from a public Claims checkout on Ubuntu 22.04 or newer.
Hermes is the only external CLI harness installed automatically. Install and
authenticate Codex CLI or Claude CLI separately before selecting either harness.
It never creates, copies, registers, or funds a Bittensor wallet.
EOF
}

if [[ $# -lt 1 ]]; then
  usage >&2
  exit 2
fi

role="$1"
shift
if [[ "${role}" != "miner" && "${role}" != "validator" ]]; then
  echo "ROLE must be miner or validator: ${role}" >&2
  exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_command="python3"
recreate_venv=false
env_file="${repo_root}/.env"
skip_system_packages=false
skip_hermes=false
reference_repo_url="https://github.com/DeSciClaims/claims-reference-miner.git"
reference_repo_version="main"
reference_dir="$(cd "${repo_root}/.." && pwd)/claims-reference-miner"
if [[ "${role}" == "miner" ]]; then
  reference_repo_url=""
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      python_command="$2"
      shift 2
      ;;
    --recreate-venv)
      recreate_venv=true
      shift
      ;;
    --env-file)
      env_file="$2"
      shift 2
      ;;
    --skip-system-packages)
      skip_system_packages=true
      shift
      ;;
    --skip-hermes)
      skip_hermes=true
      shift
      ;;
    --reference-repo-url)
      reference_repo_url="$2"
      shift 2
      ;;
    --reference-repo-version)
      reference_repo_version="$2"
      shift 2
      ;;
    --reference-dir)
      reference_dir="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "${role}" == "miner" && -n "${reference_repo_url}" ]]; then
  echo "Reference miner options are valid only for validator installation." >&2
  exit 2
fi
if [[ ! -f "${repo_root}/requirements.txt" || ! -f "${repo_root}/pyproject.toml" ]]; then
  echo "Could not locate the Claims repository root: ${repo_root}" >&2
  exit 1
fi

if [[ "${skip_system_packages}" != true ]]; then
  if [[ ! -f /etc/os-release ]]; then
    echo "Automatic system-package installation requires Ubuntu or Debian." >&2
    exit 1
  fi
  # shellcheck disable=SC1091
  source /etc/os-release
  if [[ "${ID:-}" != "ubuntu" && "${ID:-}" != "debian" && "${ID_LIKE:-}" != *debian* ]]; then
    echo "Unsupported distribution for automatic package installation: ${ID:-unknown}" >&2
    exit 1
  fi
  apt_command=(apt-get)
  if [[ ${EUID} -ne 0 ]]; then
    if ! command -v sudo >/dev/null 2>&1; then
      echo "sudo is required to install system packages." >&2
      exit 1
    fi
    apt_command=(sudo apt-get)
  fi
  "${apt_command[@]}" update
  DEBIAN_FRONTEND=noninteractive "${apt_command[@]}" install -y \
    build-essential \
    ca-certificates \
    curl \
    git \
    libgl1 \
    libglib2.0-0 \
    poppler-utils \
    python3 \
    python3-dev \
    python3-pip \
    python3-venv
fi

if ! command -v "${python_command}" >/dev/null 2>&1; then
  echo "Python command not found: ${python_command}" >&2
  exit 1
fi
"${python_command}" -c '
import sys
if sys.version_info < (3, 11):
    raise SystemExit(
        "Claims requires Python 3.11 or newer; rerun with --python python3.11 "
        "or --python python3.12"
    )
'

venv_dir="${repo_root}/.venv"
requested_python_version="$("${python_command}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ -x "${venv_dir}/bin/python" && "${recreate_venv}" != true ]]; then
  existing_python_version="$("${venv_dir}/bin/python" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  if [[ "${existing_python_version}" != "${requested_python_version}" ]]; then
    echo ".venv uses Python ${existing_python_version}, but ${python_command} is Python ${requested_python_version}." >&2
    echo "Rerun with --recreate-venv to rebuild it explicitly." >&2
    exit 1
  fi
fi
venv_args=()
if [[ "${recreate_venv}" == true ]]; then
  venv_args+=(--clear)
fi
"${python_command}" -m venv "${venv_args[@]}" "${venv_dir}"
venv_python="${repo_root}/.venv/bin/python"
"${venv_python}" -m pip install --upgrade pip setuptools wheel
"${venv_python}" -m pip install -r "${repo_root}/requirements.txt"
"${venv_python}" -m pip install --no-deps -e "${repo_root}"

if [[ "${skip_hermes}" != true ]]; then
  hermes_path="$(command -v hermes || true)"
  if [[ -z "${hermes_path}" && -x "${HOME}/.local/bin/hermes" ]]; then
    hermes_path="${HOME}/.local/bin/hermes"
  fi
  if [[ -z "${hermes_path}" ]]; then
    installer="$(mktemp "${TMPDIR:-/tmp}/claims-hermes-install.XXXXXX")"
    trap 'rm -f "${installer}"' EXIT
    curl -fsSL https://hermes-agent.nousresearch.com/install.sh -o "${installer}"
    bash "${installer}" --skip-setup --skip-browser
    rm -f "${installer}"
    trap - EXIT
  fi
fi

env_template="${repo_root}/examples/${role}.env.example"
if [[ ! -f "${env_file}" ]]; then
  install -m 0600 "${env_template}" "${env_file}"
  echo "Created ${env_file} from ${env_template}."
else
  echo "Keeping existing environment file ${env_file}."
fi

if [[ "${role}" == "validator" ]]; then
  "${venv_python}" - "${env_file}" "${repo_root}" <<'PY'
import sys
from pathlib import Path

env_path = Path(sys.argv[1])
repo_root = Path(sys.argv[2]).resolve()
key = "CLAIMS_REFERENCE_MINER_CLAIMS_REPO"
replacement = f"{key}={repo_root}"
lines = env_path.read_text(encoding="utf-8").splitlines()
found = False
for index, line in enumerate(lines):
    if line.strip().startswith(f"{key}="):
        found = True
        if not line.split("=", 1)[1].strip():
            lines[index] = replacement
if not found:
    lines.extend(["", replacement])
env_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
PY
  chmod 0600 "${env_file}"
fi

if [[ "${skip_hermes}" != true ]]; then
  hermes_path="$(command -v hermes || true)"
  if [[ -z "${hermes_path}" ]]; then
    hermes_path="${HOME}/.local/bin/hermes"
  fi
  mapfile -t hermes_settings < <(
    "${venv_python}" - "${env_file}" "${role}" <<'PY'
import sys

from dotenv import dotenv_values

values = {key: str(value or "").strip() for key, value in dotenv_values(sys.argv[1]).items()}
role = sys.argv[2]
provider = values.get("HERMES_PROVIDER")
model = values.get("HERMES_MODEL")
base_url = values.get("HERMES_BASE_URL")
if role == "validator":
    provider = provider or values.get("CLAIMS_RIGOR_PROVIDER") or values.get("CLAIMS_SILVER_FILE_AGENT_PROVIDER")
    model = model or values.get("CLAIMS_RIGOR_MODEL") or values.get("CLAIMS_SILVER_FILE_AGENT_COMPARISON_MODEL")
    base_url = base_url or values.get("CLAIMS_RIGOR_API_BASE")
else:
    provider = provider or values.get("SUBNET_CLAIMS_AGENT_PROVIDER")
    model = model or values.get("SUBNET_CLAIMS_AGENT_MODEL") or values.get("CLAIMS_AGENT_MODEL")
provider = provider or "openrouter"
model = model or "deepseek/deepseek-v4-flash"
if not base_url and provider == "openrouter":
    base_url = values.get("OPENROUTER_API_BASE") or "https://openrouter.ai/api/v1"
if not base_url and provider == "chutes":
    base_url = values.get("CHUTES_API_BASE") or "https://llm.chutes.ai/v1"
print(provider)
print(model)
print(base_url or "")
PY
  )
  hermes_provider="${hermes_settings[0]:-openrouter}"
  hermes_model="${hermes_settings[1]:-deepseek/deepseek-v4-flash}"
  hermes_base_url="${hermes_settings[2]:-}"
  if [[ "${hermes_provider}" == "chutes" ]]; then
    "${hermes_path}" config set providers.chutes.name "Chutes"
    "${hermes_path}" config set providers.chutes.base_url "${hermes_base_url:-https://llm.chutes.ai/v1}"
    "${hermes_path}" config set providers.chutes.key_env "CHUTES_API_KEY"
    "${hermes_path}" config set providers.chutes.transport "openai_chat"
  fi
  "${hermes_path}" config set model.provider "${hermes_provider}"
  "${hermes_path}" config set model.default "${hermes_model}"
  if [[ -n "${hermes_base_url}" ]]; then
    "${hermes_path}" config set model.base_url "${hermes_base_url}"
  fi
  echo "Configured Hermes provider=${hermes_provider} model=${hermes_model}."
fi

mkdir -p "${repo_root}/outputs/${role}" "${repo_root}/.cache"

if [[ "${role}" == "validator" && -n "${reference_repo_url}" ]]; then
  if [[ -d "${reference_dir}/.git" ]]; then
    git -C "${reference_dir}" remote set-url origin "${reference_repo_url}"
    git -C "${reference_dir}" fetch origin "${reference_repo_version}"
  elif [[ -e "${reference_dir}" ]]; then
    echo "Reference destination exists but is not a Git checkout: ${reference_dir}" >&2
    exit 1
  else
    git clone --no-checkout "${reference_repo_url}" "${reference_dir}"
    git -C "${reference_dir}" fetch origin "${reference_repo_version}"
  fi
  git -C "${reference_dir}" checkout --detach FETCH_HEAD
  "${venv_python}" -m pip install "${reference_dir}"
fi

"${venv_python}" -c 'import bittensor, dspy, pdf_inspector; print("Claims runtime OK")'
if [[ "${role}" == "validator" && -n "${reference_repo_url}" ]]; then
  "${venv_python}" -m dotenv -f "${env_file}" run --override -- \
    "${venv_python}" - <<'PY'
from claims_reference_miner.config import ReferenceMinerConfig
from claims_reference_miner.runner import _ensure_claims_importable

config = ReferenceMinerConfig.from_env()
claims_repo = config.claims_repo.resolve()
_ensure_claims_importable(claims_repo)
from miner.agent_v1.config import AgentV1Config  # noqa: E402,F401

print(f"Reference miner runtime OK; Claims repo={claims_repo}")
PY
fi
if [[ "${skip_hermes}" != true ]]; then
  hermes_path="$(command -v hermes || true)"
  if [[ -z "${hermes_path}" ]]; then
    hermes_path="${HOME}/.local/bin/hermes"
  fi
  "${hermes_path}" --help >/dev/null
fi

echo "Claims ${role} installation completed."
echo "Edit ${env_file}, then follow the ${role} neuron command in README.md."
