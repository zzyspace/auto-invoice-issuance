#!/usr/bin/env bash

set -euo pipefail

SERVER="${1:-root@139.196.140.215}"
REPO_DIR="/opt/auto-invoice-issuance/current"
VENV_DIR="/opt/auto-invoice-issuance/venv"
SERVICE_NAME="auto-invoice-issuance.service"
GITHUB_KEY="/home/wechatclaw/.ssh/id_ed25519_github_wechat_claw"
KNOWN_HOSTS="/home/wechatclaw/.ssh/known_hosts"

SSH_OPTS=(
  -o BatchMode=yes
  -o StrictHostKeyChecking=no
  -o ConnectTimeout=10
)

REMOTE_SCRIPT=$(cat <<'EOF'
set -euo pipefail

REPO_DIR="/opt/auto-invoice-issuance/current"
VENV_DIR="/opt/auto-invoice-issuance/venv"
SERVICE_NAME="auto-invoice-issuance.service"
GITHUB_KEY="/home/wechatclaw/.ssh/id_ed25519_github_wechat_claw"
KNOWN_HOSTS="/home/wechatclaw/.ssh/known_hosts"

export GIT_SSH_COMMAND="ssh -i ${GITHUB_KEY} -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=${KNOWN_HOSTS} -o ConnectTimeout=10"

git -C "${REPO_DIR}" pull --ff-only origin main
"${VENV_DIR}/bin/pip" install -r "${REPO_DIR}/requirements.txt"
systemctl restart "${SERVICE_NAME}"
systemctl status --no-pager --lines=20 "${SERVICE_NAME}"
EOF
)

ssh "${SSH_OPTS[@]}" "${SERVER}" "${REMOTE_SCRIPT}"
