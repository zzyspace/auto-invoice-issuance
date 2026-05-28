#!/usr/bin/env bash

set -euo pipefail

DEFAULT_SERVER="root@139.196.140.215"
SERVER="${1:-${DEFAULT_SERVER}}"
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

run_release() {
  export GIT_SSH_COMMAND="ssh -i ${GITHUB_KEY} -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=${KNOWN_HOSTS} -o ConnectTimeout=10"

  git -C "${REPO_DIR}" pull --ff-only origin main
  "${VENV_DIR}/bin/pip" install -r "${REPO_DIR}/requirements.txt"
  systemctl restart "${SERVICE_NAME}"
  systemctl status --no-pager --lines=20 "${SERVICE_NAME}"
}

if [[ "${SERVER}" == "local" || "${SERVER}" == "localhost" ]]; then
  run_release
elif [[ -d "${REPO_DIR}/.git" && "${SERVER}" == "${DEFAULT_SERVER}" ]]; then
  run_release
else
  ssh "${SSH_OPTS[@]}" "${SERVER}" "$(declare -f run_release); REPO_DIR='${REPO_DIR}'; VENV_DIR='${VENV_DIR}'; SERVICE_NAME='${SERVICE_NAME}'; GITHUB_KEY='${GITHUB_KEY}'; KNOWN_HOSTS='${KNOWN_HOSTS}'; run_release"
fi
