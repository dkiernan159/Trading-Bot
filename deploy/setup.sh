#!/usr/bin/env bash
# Provisions a fresh Ubuntu Hetzner VPS to run the trading bot as a systemd
# service under a dedicated non-root user, with an SSH-only firewall.
#
# Run as root (or via sudo) on the server, right after first boot:
#   curl -fsSL https://raw.githubusercontent.com/dkiernan159/trading-bot/claude/topstepx-trading-bot-wqy0at/deploy/setup.sh -o setup.sh
#   bash setup.sh
#
# Safe to re-run -- steps are idempotent.

set -euo pipefail

REPO_URL="https://github.com/dkiernan159/trading-bot.git"
REPO_BRANCH="claude/topstepx-trading-bot-wqy0at"
SERVICE_USER="tradingbot"
INSTALL_DIR="/home/${SERVICE_USER}/trading-bot"

if [[ $EUID -ne 0 ]]; then
  echo "Run this as root (or with sudo)." >&2
  exit 1
fi

echo "==> Updating system packages"
apt-get update -y
apt-get upgrade -y

echo "==> Installing Python, git, ufw"
apt-get install -y python3 python3-venv python3-pip git ufw

echo "==> Creating dedicated service user (${SERVICE_USER})"
if ! id "${SERVICE_USER}" &>/dev/null; then
  useradd --create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

echo "==> Cloning/updating the bot repo"
if [[ -d "${INSTALL_DIR}/.git" ]]; then
  sudo -u "${SERVICE_USER}" git -C "${INSTALL_DIR}" fetch origin "${REPO_BRANCH}"
  sudo -u "${SERVICE_USER}" git -C "${INSTALL_DIR}" checkout "${REPO_BRANCH}"
  sudo -u "${SERVICE_USER}" git -C "${INSTALL_DIR}" pull origin "${REPO_BRANCH}"
else
  sudo -u "${SERVICE_USER}" git clone --branch "${REPO_BRANCH}" "${REPO_URL}" "${INSTALL_DIR}"
fi

echo "==> Setting up the virtualenv"
sudo -u "${SERVICE_USER}" python3 -m venv "${INSTALL_DIR}/.venv"
sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/.venv/bin/pip" install --upgrade pip
sudo -u "${SERVICE_USER}" "${INSTALL_DIR}/.venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"

echo "==> Preparing .env and logs/"
if [[ ! -f "${INSTALL_DIR}/.env" ]]; then
  sudo -u "${SERVICE_USER}" cp "${INSTALL_DIR}/.env.example" "${INSTALL_DIR}/.env"
  echo "    Created ${INSTALL_DIR}/.env -- edit it now with your ProjectX credentials."
fi
chmod 600 "${INSTALL_DIR}/.env"
chown "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}/.env"
sudo -u "${SERVICE_USER}" mkdir -p "${INSTALL_DIR}/logs"

echo "==> Installing systemd service"
cp "${INSTALL_DIR}/deploy/trading-bot.service" /etc/systemd/system/trading-bot.service
systemctl daemon-reload
systemctl enable trading-bot
# Not started automatically -- .env needs real credentials filled in first.

echo "==> Installing log rotation"
cp "${INSTALL_DIR}/deploy/trading-bot.logrotate" /etc/logrotate.d/trading-bot

echo "==> Configuring firewall (SSH only -- the bot only makes outbound connections)"
ufw allow OpenSSH
ufw --force enable

cat <<EOF

==> Done.

Next steps:
  1. Edit ${INSTALL_DIR}/.env with your PROJECTX_USERNAME / PROJECTX_API_KEY / PROJECTX_ACCOUNT_ID.
  2. Sanity-check with dry_run first (config.yaml already defaults to dry_run: true):
       sudo -u ${SERVICE_USER} bash -c 'cd ${INSTALL_DIR} && source .venv/bin/activate && python -m src.runner'
     Watch it log the orders it *would* place, then Ctrl-C.
  3. When you're ready: sudo systemctl start trading-bot
  4. Watch logs:  journalctl -u trading-bot -f    or    tail -f ${INSTALL_DIR}/logs/bot.log
EOF
