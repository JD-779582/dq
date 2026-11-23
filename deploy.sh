#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${NEXORADAR_DIR:-/opt/nexoradar}"
DOWNLOAD_URL='https://media.meilinvps.com/f.php?id=nkNmLE7eAcpc.nexoradar'
ADD_COUNT="${1:-1}"

if (( EUID != 0 )); then
  echo "Run this script as root, for example: sudo $0 [container-count]" >&2
  exit 1
fi

if ! [[ "$ADD_COUNT" =~ ^[1-9][0-9]*$ ]] || (( ADD_COUNT > 100 )); then
  echo "Usage: $0 [containers-to-add] (1-100, default: 1)" >&2
  exit 2
fi

install_missing_packages() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1 && command -v curl >/dev/null 2>&1; then
    echo 'Docker, Docker Compose, and curl are already installed.'
    return
  fi

  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    packages=()
    command -v docker >/dev/null 2>&1 || packages+=(docker.io)
    docker compose version >/dev/null 2>&1 || packages+=(docker-compose-v2)
    command -v curl >/dev/null 2>&1 || packages+=(curl)
    ((${#packages[@]} == 0)) || apt-get install -y "${packages[@]}"
  elif command -v dnf >/dev/null 2>&1; then
    packages=()
    command -v docker >/dev/null 2>&1 || packages+=(docker)
    docker compose version >/dev/null 2>&1 || packages+=(docker-compose-plugin)
    command -v curl >/dev/null 2>&1 || packages+=(curl)
    ((${#packages[@]} == 0)) || dnf install -y "${packages[@]}"
  elif command -v yum >/dev/null 2>&1; then
    packages=()
    command -v docker >/dev/null 2>&1 || packages+=(docker)
    docker compose version >/dev/null 2>&1 || packages+=(docker-compose-plugin)
    command -v curl >/dev/null 2>&1 || packages+=(curl)
    ((${#packages[@]} == 0)) || yum install -y "${packages[@]}"
  else
    echo 'Unsupported package manager. Install Docker Engine, the Compose plugin, and curl, then rerun.' >&2
    exit 1
  fi

  if command -v systemctl >/dev/null 2>&1; then
    systemctl enable --now docker
  fi
  command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1 && command -v curl >/dev/null 2>&1 || {
    echo 'Automatic installation did not provide Docker, the Compose plugin, and curl.' >&2
    exit 1
  }
}

install_missing_packages

configure_registry_mirror() {
  local config=/etc/docker/daemon.json
  mkdir -p /etc/docker

  if [[ -f "$config" ]]; then
    cp -a "$config" "$config.bak.$(date +%Y%m%d%H%M%S)"
  fi

  python3 - "$config" <<'PY'
import json
import os
import sys

path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as config_file:
        config = json.load(config_file)
except FileNotFoundError:
    config = {}

mirrors = config.setdefault("registry-mirrors", [])
mirror = "https://docker.m.daocloud.io"
if mirror not in mirrors:
    mirrors.append(mirror)

temporary_path = path + ".tmp"
with open(temporary_path, "w", encoding="utf-8") as config_file:
    json.dump(config, config_file, indent=2)
    config_file.write("\n")
os.replace(temporary_path, path)
PY

  if command -v systemctl >/dev/null 2>&1; then
    systemctl restart docker
  fi
}

configure_registry_mirror

mkdir -p "$APP_DIR"
cd "$APP_DIR"

EXISTING_COUNT=0
if [[ -f compose.yaml ]]; then
  EXISTING_COUNT="$(sed -nE 's/^  nexoradar-([0-9]+):$/\1/p' compose.yaml | sort -n | tail -n 1)"
  EXISTING_COUNT="${EXISTING_COUNT:-0}"
fi
COUNT=$((EXISTING_COUNT + ADD_COUNT))
if (( COUNT > 100 )); then
  echo "This deployment would reach $COUNT containers, above the current limit of 100." >&2
  exit 2
fi
echo "Adding $ADD_COUNT container(s). Total target: $COUNT."

if [[ ! -s nexoradar ]]; then
  curl -fL "$DOWNLOAD_URL" -o nexoradar
fi
chmod +x nexoradar

cat > Dockerfile <<'DOCKERFILE'
FROM debian:bookworm-slim
WORKDIR /opt/nexoradar
COPY nexoradar ./nexoradar
RUN chmod 0755 ./nexoradar
EXPOSE 9000
CMD ["./nexoradar"]
DOCKERFILE

{
  printf 'services:\n'
  for ((i = 1; i <= COUNT; i++)); do
    port=$((9000 + i))
    printf '  nexoradar-%d:\n' "$i"
    printf '    build: .\n'
    printf '    container_name: nexoradar-%d\n' "$i"
    printf '    restart: unless-stopped\n'
    printf '    ports:\n'
    printf '      - "%d:9000"\n' "$port"
  done
} > compose.yaml

PANEL_DIR="${PANEL_DIR:-/opt/nexoradar-panel}"
mkdir -p "$PANEL_DIR/data"
curl -fL 'https://raw.githubusercontent.com/JD-779582/dq/main/panel.py' -o "$PANEL_DIR/panel.py"
docker build -t nexoradar:latest "$APP_DIR"
docker compose up -d --build
docker compose ps
if [[ ! -f "$PANEL_DIR/.env" ]]; then
  PANEL_PASSWORD="$(openssl rand -hex 24)"
  printf 'ADMIN_PASSWORD=%s\n' "$PANEL_PASSWORD" > "$PANEL_DIR/.env"
  chmod 600 "$PANEL_DIR/.env"
else
  PANEL_PASSWORD="$(sed -n 's/^ADMIN_PASSWORD=//p' "$PANEL_DIR/.env" | tail -n 1)"
fi
cat > "$PANEL_DIR/compose.yaml" <<'PANEL_COMPOSE'
services:
  panel:
    image: python:3.12-slim
    container_name: nexoradar-panel
    restart: unless-stopped
    command: ["python", "/app/panel.py"]
    environment:
      ADMIN_PASSWORD: ${ADMIN_PASSWORD}
      PUBLIC_IP: ${PUBLIC_IP}
      PANEL_DB: /data/panel.db
      PANEL_PORT: "8080"
    ports:
      - "8080:8080"
    volumes:
      - ./panel.py:/app/panel.py:ro
      - ./data:/data
      - /var/run/docker.sock:/var/run/docker.sock
PANEL_COMPOSE

PUBLIC_IP=''
for ip_service in https://api.ipify.org https://ipv4.icanhazip.com https://ifconfig.me/ip; do
  PUBLIC_IP="$(curl -4fsS --max-time 5 "$ip_service" 2>/dev/null | tr -d '[:space:]' || true)"
  if [[ "$PUBLIC_IP" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    break
  fi
  PUBLIC_IP=''
done

echo
echo 'Public addresses:'
if [[ -z "$PUBLIC_IP" ]]; then
  echo 'Could not detect the public IPv4 address. Check outbound HTTPS or set PUBLIC_IP before running this script.' >&2
  PUBLIC_IP='PUBLIC_IP_UNKNOWN'
fi
if ! grep -q '^PUBLIC_IP=' "$PANEL_DIR/.env"; then
  printf 'PUBLIC_IP=%s\n' "$PUBLIC_IP" >> "$PANEL_DIR/.env"
else
  sed -i "s/^PUBLIC_IP=.*/PUBLIC_IP=$PUBLIC_IP/" "$PANEL_DIR/.env"
fi
(cd "$PANEL_DIR" && docker compose up -d)
for ((i = 1; i <= COUNT; i++)); do
  port=$((9000 + i))
  printf '  nexoradar-%d  ->  %s:%d\n' "$i" "$PUBLIC_IP" "$port"
done
echo "  Panel -> http://${PUBLIC_IP}:8080"
echo "  Admin password -> ${PANEL_PASSWORD}"
