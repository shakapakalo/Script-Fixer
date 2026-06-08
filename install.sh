#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────
#  Artlist Image API — ONE-CLICK INSTALL
#  Run once on a fresh VPS:  bash install.sh
# ─────────────────────────────────────────────────────────────────
set -e
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE="artlist-api"
PORT=9222
VENV="$REPO_DIR/.venv"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log()  { echo -e "${GREEN}[install]${NC} $*"; }
warn() { echo -e "${YELLOW}[warn]${NC} $*"; }
die()  { echo -e "${RED}[error]${NC} $*"; exit 1; }

log "══════════════════════════════════════════"
log "  Artlist API — Install Script"
log "  Dir : $REPO_DIR"
log "  Port: $PORT"
log "══════════════════════════════════════════"

# ── 1. Kill anything already on port 9222 ────────────────────────
log "Stopping any process on port $PORT ..."
fuser -k ${PORT}/tcp 2>/dev/null && log "Killed old process on :$PORT" || log "Port $PORT was free"
if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
    systemctl stop "$SERVICE"
    log "Stopped systemd service: $SERVICE"
fi

# ── 2. System packages ───────────────────────────────────────────
log "Installing system packages ..."
apt-get update -qq
apt-get install -y -qq \
    python3 python3-pip python3-venv python3-full \
    curl git libssl-dev libffi-dev build-essential

# ── 3. Create virtual environment ────────────────────────────────
log "Creating Python venv at $VENV ..."
python3 -m venv "$VENV"
log "Venv created."

# ── 4. Install Python packages inside venv ───────────────────────
log "Installing Python packages into venv ..."
"$VENV/bin/pip" install --upgrade pip --quiet
"$VENV/bin/pip" install flask gunicorn requests curl_cffi Pillow --quiet
log "Python packages installed."

# ── 5. Create systemd service ────────────────────────────────────
log "Creating systemd service: /etc/systemd/system/${SERVICE}.service"

cat > /etc/systemd/system/${SERVICE}.service << EOF
[Unit]
Description=Artlist Image-to-Image API
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
User=root
WorkingDirectory=${REPO_DIR}
ExecStart=${VENV}/bin/gunicorn api:app \\
    --bind 0.0.0.0:${PORT} \\
    --workers 1 \\
    --threads 4 \\
    --timeout 300 \\
    --graceful-timeout 30 \\
    --keep-alive 5 \\
    --log-level info \\
    --access-logfile -
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE}

[Install]
WantedBy=multi-user.target
EOF

# ── 6. Enable & start service ────────────────────────────────────
systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl start  "$SERVICE"

# ── 7. Wait and check ────────────────────────────────────────────
sleep 3
if systemctl is-active --quiet "$SERVICE"; then
    log "══════════════════════════════════════════"
    log "  ✓ Service started successfully!"
    log "  ✓ API running at http://0.0.0.0:${PORT}"
    log ""
    log "  Commands:"
    log "    systemctl status $SERVICE   # check status"
    log "    journalctl -u $SERVICE -f   # live logs"
    log "    bash update.sh              # pull & restart"
    log "══════════════════════════════════════════"
else
    die "Service failed to start! Check: journalctl -u $SERVICE -n 50"
fi

# ── 8. Quick health check ────────────────────────────────────────
sleep 2
if curl -sf http://localhost:${PORT}/health > /dev/null 2>&1; then
    log "✓ Health check passed — API is responding."
else
    warn "Health check failed (API might still be starting up). Check logs:"
    warn "  journalctl -u $SERVICE -n 30"
fi
