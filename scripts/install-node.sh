#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: install-node.sh ROLE [options]

ROLE is miner or validator.

Options:
  --python COMMAND               Python command (default: python3)
  --env-file FILE               Runtime environment file (default: .env)
  --skip-system-packages        Do not install Ubuntu packages
  --skip-hermes                 Do not install Hermes Agent
  --reference-repo-url URL      Private reference repository SSH URL (validator only)
  --reference-repo-version REF  Reference branch, tag, or commit (default: main)
  --reference-key FILE          Approved read-only SSH deploy key (validator only)
  --reference-known-hosts FILE  Pinned SSH known_hosts file
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
env_file="${repo_root}/.env"
skip_system_packages=false
skip_hermes=false
reference_repo_url=""
reference_repo_version="main"
reference_key=""
reference_known_hosts="${HOME}/.ssh/known_hosts"
reference_dir="$(cd "${repo_root}/.." && pwd)/claims-reference-miner"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      python_command="$2"
      shift 2
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
    --reference-key)
      reference_key="$2"
      shift 2
      ;;
    --reference-known-hosts)
      reference_known_hosts="$2"
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
if sys.version_info < (3, 10):
    raise SystemExit("Claims requires Python 3.10 or newer")
'

"${python_command}" -m venv "${repo_root}/.venv"
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
    base_url = base_url or values.get("CLAIMS_RIGOR_API_BASE") or values.get("OPENROUTER_API_BASE")
else:
    provider = provider or values.get("SUBNET_CLAIMS_AGENT_PROVIDER")
    model = model or values.get("SUBNET_CLAIMS_AGENT_MODEL") or values.get("CLAIMS_AGENT_MODEL")
    base_url = base_url or values.get("OPENROUTER_API_BASE")
provider = provider or "openrouter"
model = model or "deepseek/deepseek-v4-flash"
if not base_url and provider == "openrouter":
    base_url = "https://openrouter.ai/api/v1"
print(provider)
print(model)
print(base_url or "")
PY
  )
  hermes_provider="${hermes_settings[0]:-openrouter}"
  hermes_model="${hermes_settings[1]:-deepseek/deepseek-v4-flash}"
  hermes_base_url="${hermes_settings[2]:-}"
  "${hermes_path}" config set model.provider "${hermes_provider}"
  "${hermes_path}" config set model.default "${hermes_model}"
  if [[ -n "${hermes_base_url}" ]]; then
    "${hermes_path}" config set model.base_url "${hermes_base_url}"
  fi
  echo "Configured Hermes provider=${hermes_provider} model=${hermes_model}."
fi

mkdir -p "${repo_root}/outputs/${role}" "${repo_root}/.cache"

if [[ "${role}" == "validator" && -n "${reference_repo_url}" ]]; then
  if [[ ! -r "${reference_key}" ]]; then
    echo "--reference-key must name an approved read-only deploy key." >&2
    exit 2
  fi
  if [[ ! -r "${reference_known_hosts}" ]]; then
    echo "Pinned known_hosts file is not readable: ${reference_known_hosts}" >&2
    exit 2
  fi
  chmod 0600 "${reference_key}"
  git_ssh_command="ssh -i ${reference_key} -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=${reference_known_hosts}"
  if [[ -d "${reference_dir}/.git" ]]; then
    GIT_SSH_COMMAND="${git_ssh_command}" git -C "${reference_dir}" fetch origin "${reference_repo_version}"
  elif [[ -e "${reference_dir}" ]]; then
    echo "Reference destination exists but is not a Git checkout: ${reference_dir}" >&2
    exit 1
  else
    GIT_SSH_COMMAND="${git_ssh_command}" git clone --no-checkout \
      "${reference_repo_url}" "${reference_dir}"
    GIT_SSH_COMMAND="${git_ssh_command}" git -C "${reference_dir}" fetch origin "${reference_repo_version}"
  fi
  git -C "${reference_dir}" checkout --detach FETCH_HEAD
  "${venv_python}" -m pip install "${reference_dir}"
fi

"${venv_python}" -c 'import bittensor, dspy, pdf_inspector; print("Claims runtime OK")'
if [[ "${role}" == "validator" && -n "${reference_repo_url}" ]]; then
  "${venv_python}" -c 'import claims_reference_miner; print("Reference miner runtime OK")'
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
