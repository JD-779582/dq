#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${NEXORADAR_DIR:-/opt/nexoradar}"
DOWNLOAD_URL='https://media.meilinvps.com/f.php?id=nkNmLE7eAcpc.nexoradar'
COUNT="${1:-1}"

if (( EUID != 0 )); then
  echo "Run this script as root, for example: sudo $0 [container-count]" >&2
  exit 1
fi

if ! [[ "$COUNT" =~ ^[1-9][0-9]*$ ]] || (( COUNT > 100 )); then
  echo "Usage: $0 [container-count] (1-100, default: 1)" >&2
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

docker compose up -d --build
docker compose ps

echo
echo 'Port mappings:'
for ((i = 1; i <= COUNT; i++)); do
  port=$((9000 + i))
  printf '  nexoradar-%d  ->  0.0.0.0:%d\n' "$i" "$port"
done
