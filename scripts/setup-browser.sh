#!/usr/bin/env bash
#
# Install the display stack that hosts trellm's headed Chrome.
# Run this once after a fresh install or when bringing up a new host.
#
# Components:
# - Xvfb: virtual display for headed Chrome
# - x11vnc + noVNC + websockify: remote viewing/control of the browser
# - Google Chrome: the real headed browser the patchright-mcp-lite MCP
#   server attaches to via CDP at localhost:9222 (see docs/patchright-mcp.md)
#
# Idempotent: safe to re-run. apt-get install is a no-op if packages already
# present. Most boxes that ran the prior browser-stack experiment will
# already have everything installed.
#
set -euo pipefail

log() {
  echo "[$(date -Iseconds)] [setup-browser] $*"
}

log "Installing Xvfb, x11vnc, novnc, websockify..."
sudo apt-get update -qq
sudo apt-get install -y -qq xvfb x11vnc novnc websockify

if ! command -v google-chrome >/dev/null 2>&1; then
  log "Installing Google Chrome (stable)..."
  sudo curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
    | sudo gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg
  echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
    | sudo tee /etc/apt/sources.list.d/google-chrome.list >/dev/null
  sudo apt-get update -qq
  sudo apt-get install -y -qq google-chrome-stable
else
  log "google-chrome already installed: $(google-chrome --version)"
fi

log ""
log "Setup complete. Next steps:"
log "  bash scripts/start-browser.sh start    # bring the stack up"
log "  http://\$(hostname):6080/vnc.html       # view the browser remotely"
