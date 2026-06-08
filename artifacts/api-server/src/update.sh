#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────
#  Artlist Image API — ONE-CLICK UPDATE
#  Run after pushing new code to GitHub:  bash update.sh
# ─────────────────────────────────────────────────────────────────
set -e
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE="artlist-api"
PORT=9222

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
log()  { echo -e "${GREEN}[update]${NC} $*"; }
warn() { echo -e "${YELLOW}[warn]${NC} $*"; }
die()  { echo -e "${RED}[error]${NC} $*"; exit 1; }

log "══════════════════════════════════════════"
log "  Artlist API — Update Script"
log "  Dir: $REPO_DIR"
log "══════════════════════════════════════════"

cd "$REPO_DIR"

# ── 1. Git pull ──────────────────────────────────────────────────
log "Pulling latest code from GitHub ..."
git fetch origin
BEFORE=$(git rev-parse HEAD)
git pull origin main
AFTER=$(git rev-parse HEAD)

if [ "$BEFORE" = "$AFTER" ]; then
    warn "No new commits. Code is already up to date."
else
    CHANGED=$(git diff --name-only "$BEFORE" "$AFTER")
    log "Updated files:"
    echo "$CHANGED" | while read f; do log "  • $f"; done
fi

# ── 2. Update Python packages if requirements.txt changed ────────
if git diff --name-only "$BEFORE" "$AFTER" | grep -q "requirements.txt"; then
    log "requirements.txt changed — installing new packages ..."
    pip3 install -r requirements.txt --quiet
    log "Packages updated."
else
    log "requirements.txt unchanged — skipping pip install."
fi

# ── 3. Restart service ───────────────────────────────────────────
log "Restarting service: $SERVICE ..."
if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
    systemctl restart "$SERVICE"
else
    warn "Service not found via systemd. Trying to kill old process and start fresh ..."
    fuser -k ${PORT}/tcp 2>/dev/null || true
    sleep 1
    cd "$REPO_DIR"
    nohup gunicorn api:app \
        --bind 0.0.0.0:${PORT} \
        --workers 1 \
        --threads 4 \
        --timeout 300 \
        --log-level info \
        --access-logfile - \
        > /tmp/artlist-api.log 2>&1 &
    log "Started gunicorn (PID=$!). Logs: tail -f /tmp/artlist-api.log"
fi

# ── 4. Wait and verify ───────────────────────────────────────────
sleep 3
if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
    log "✓ Service is running."
else
    warn "Service status unknown."
fi

sleep 2
if curl -sf http://localhost:${PORT}/health > /dev/null 2>&1; then
    HEALTH=$(curl -s http://localhost:${PORT}/health)
    log "══════════════════════════════════════════"
    log "  ✓ Update complete! API is responding."
    log "  Health: $HEALTH"
    log "══════════════════════════════════════════"
else
    warn "Health check failed. Check logs:"
    warn "  journalctl -u $SERVICE -n 30"
    warn "  or: tail -f /tmp/artlist-api.log"
    exit 1
fi
