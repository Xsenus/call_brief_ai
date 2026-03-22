#!/usr/bin/env bash
set -Eeuo pipefail

umask 027

RELEASE_ARCHIVE="${1:?Usage: remote_deploy.sh <release-archive> <release-id> <service-file>}"
RELEASE_ID="${2:-manual-$(date -u +%Y%m%d%H%M%S)}"
SERVICE_SOURCE="${3:-/tmp/callbot.service}"

APP_NAME="${APP_NAME:-call_brief_ai}"
APP_USER="${APP_USER:-callbot}"
APP_GROUP="${APP_GROUP:-$APP_USER}"
APP_BASE_DIR="${APP_BASE_DIR:-/opt/${APP_NAME}}"
SERVICE_NAME="${SERVICE_NAME:-callbot.service}"
RELEASES_DIR="${APP_BASE_DIR}/releases"
SHARED_DIR="${APP_BASE_DIR}/shared"
CURRENT_LINK="${APP_BASE_DIR}/current"
RELEASE_DIR="${RELEASES_DIR}/${RELEASE_ID}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "This deploy script must run as root." >&2
  exit 1
fi

if [[ ! -f "${RELEASE_ARCHIVE}" ]]; then
  echo "Release archive not found: ${RELEASE_ARCHIVE}" >&2
  exit 1
fi

if [[ ! -f "${SERVICE_SOURCE}" ]]; then
  echo "Service file not found: ${SERVICE_SOURCE}" >&2
  exit 1
fi

command -v python3 >/dev/null 2>&1 || { echo "python3 is required"; exit 1; }
command -v ffmpeg >/dev/null 2>&1 || { echo "ffmpeg is required"; exit 1; }

if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "${APP_BASE_DIR}" --shell /bin/bash "${APP_USER}"
fi

mkdir -p "${RELEASES_DIR}" "${SHARED_DIR}/work" "${SHARED_DIR}/logs"

if [[ ! -f "${SHARED_DIR}/.env" ]]; then
  install -m 600 /dev/null "${SHARED_DIR}/.env"
fi

if [[ ! -f "${SHARED_DIR}/instructions.json" ]]; then
  install -m 644 /dev/null "${SHARED_DIR}/instructions.json"
fi

rm -rf "${RELEASE_DIR}"
mkdir -p "${RELEASE_DIR}"
tar -xzf "${RELEASE_ARCHIVE}" -C "${RELEASE_DIR}"

python3 -m venv "${RELEASE_DIR}/.venv"
"${RELEASE_DIR}/.venv/bin/pip" install --upgrade pip
"${RELEASE_DIR}/.venv/bin/pip" install -r "${RELEASE_DIR}/requirements.txt"

if [[ -f "${RELEASE_DIR}/instructions.json" ]] && [[ ! -s "${SHARED_DIR}/instructions.json" ]]; then
  cp "${RELEASE_DIR}/instructions.json" "${SHARED_DIR}/instructions.json"
fi

install -m 644 "${SERVICE_SOURCE}" "/etc/systemd/system/${SERVICE_NAME}"

ln -sfn "${RELEASE_DIR}" "${CURRENT_LINK}"
chown -h "${APP_USER}:${APP_GROUP}" "${CURRENT_LINK}"
chown -R "${APP_USER}:${APP_GROUP}" "${APP_BASE_DIR}"

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

find "${RELEASES_DIR}" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' \
  | sort -nr \
  | awk 'NR > 5 {print $2}' \
  | xargs -r rm -rf

rm -f "${RELEASE_ARCHIVE}" "${SERVICE_SOURCE}" /tmp/remote_deploy.sh

echo "Deployment finished."
echo "Current release: ${RELEASE_DIR}"
systemctl --no-pager --full status "${SERVICE_NAME}" || true
