#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: prepare-reference-access.sh [KEY_PATH] [LABEL]

Generate one validator-specific SSH key for read-only access to the private
claims-reference-miner repository. Only the public key should be shared with a
repository administrator.

Defaults:
  KEY_PATH  ~/.ssh/claims-reference-miner
  LABEL     claims-reference-validator
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

key_path="${1:-${HOME}/.ssh/claims-reference-miner}"
label="${2:-claims-reference-validator}"
mkdir -p "$(dirname "${key_path}")"
chmod 0700 "$(dirname "${key_path}")"
if [[ ! -f "${key_path}" ]]; then
  ssh-keygen -t ed25519 -N "" -f "${key_path}" -C "${label}"
fi
chmod 0600 "${key_path}"
chmod 0644 "${key_path}.pub"

echo "Send this public key to a claims-reference-miner repository administrator:"
cat "${key_path}.pub"
echo
echo "Keep the private key at ${key_path} on the validator host."
