#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="${NEXORADAR_DIR:-/opt/nexoradar}"
PANEL_DIR="${PANEL_DIR:-/opt/nexoradar-panel}"
mkdir -p "$APP_DIR" "$PANEL_DIR"
if [[ ! -s "$APP_DIR/nexoradar" ]]; then
  curl -fL 'https://media.meilinvps.com/f.php?id=nkNmLE7eAcpc.nexoradar' -o "$APP_DIR/nexoradar"
fi
chmod +x "$APP_DIR/nexoradar"
cat > "$APP_DIR/Dockerfile" <<'DOCKER'
FROM debian:bookworm-slim
WORKDIR /opt/nexoradar
COPY nexoradar ./nexoradar
RUN chmod 0755 ./nexoradar
EXPOSE 9000
CMD ["./nexoradar"]
DOCKER
docker build -t nexoradar:latest "$APP_DIR"
install -m 0644 "$DIR/panel.py" "$PANEL_DIR/panel.py"
cat > "$PANEL_DIR/compose.yaml" <<'YAML'
services:
  panel:
    image: python:3.12-slim
    container_name: nexoradar-panel
    restart: unless-stopped
    command: ["python", "/app/panel.py"]
    environment:
      ADMIN_PASSWORD: ${ADMIN_PASSWORD:?Set ADMIN_PASSWORD in .env}
      PUBLIC_IP: ${PUBLIC_IP:?Set PUBLIC_IP in .env}
      PANEL_PORT: "8080"
      PANEL_DB: /data/panel.db
    ports:
      - "8080:8080"
    volumes:
      - ./panel.py:/app/panel.py:ro
      - ./data:/data
      - /var/run/docker.sock:/var/run/docker.sock
YAML
if [[ ! -f "$PANEL_DIR/.env" ]]; then
  printf 'ADMIN_PASSWORD=%s\nPUBLIC_IP=%s\n' "$(openssl rand -hex 24)" "$(curl -4fsS --max-time 8 https://ipv4.icanhazip.com | tr -d '[:space:]')" > "$PANEL_DIR/.env"
  chmod 600 "$PANEL_DIR/.env"
fi
cd "$PANEL_DIR"
docker compose up -d
echo "Panel: http://$(grep '^PUBLIC_IP=' .env | cut -d= -f2):8080"
echo "Admin password: $(grep '^ADMIN_PASSWORD=' .env | cut -d= -f2)"
