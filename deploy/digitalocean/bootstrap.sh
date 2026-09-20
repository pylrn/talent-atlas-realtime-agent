#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/pylrn/hybrid-search.git}"
REPO_BRANCH="${REPO_BRANCH:-feat/personalization-hints}"
APP_DIR="${APP_DIR:-/opt/talent-atlas}"

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl git

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

. /etc/os-release
printf '%s\n' \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
  > /etc/apt/sources.list.d/docker.list

apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker

if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" checkout "$REPO_BRANCH"
  git -C "$APP_DIR" pull --ff-only origin "$REPO_BRANCH"
else
  git clone --branch "$REPO_BRANCH" --single-branch "$REPO_URL" "$APP_DIR"
fi

if [[ ! -f "$APP_DIR/.env.production" ]]; then
  install -m 0600 /dev/null "$APP_DIR/.env.production"
fi

cat <<EOF
Server preparation is complete.

Next:
  1. Fill $APP_DIR/.env.production with production secrets.
  2. Set APP_DOMAIN in $APP_DIR/deploy/digitalocean/.env.
  3. Run:
     cd $APP_DIR/deploy/digitalocean
     docker compose up -d --build
EOF
