#!/usr/bin/env bash
#
# Start the headed Chrome browser stack for trellm-managed projects.
# Launches Xvfb (virtual display), Chrome with CDP, x11vnc, and noVNC.
#
# patchright-mcp-lite attaches to Chrome via CDP at localhost:9222 (see
# ~/src/patchright-mcp-lite/src/connection.ts which calls
# chromium.connectOverCDP). View/control the browser via noVNC at
# http://<host>:6080/vnc.html or any VNC client on port 5900.
#
# We re-use the standard ports (5900, 6080, 9222, display :99) because
# humphrey now runs in its own Docker container with bridge networking
# and no published ports — there's no host-side conflict.
#
# Usage: scripts/start-browser.sh [start|stop|status|restart]
#
set -uo pipefail

DISPLAY_NUM=99
DISPLAY=":${DISPLAY_NUM}"
CHROME_USER_DATA="${HOME}/.chrome-trellm"
CDP_PORT=9222
VNC_PORT=5900
NOVNC_PORT=6080
PID_DIR="${HOME}/.browser-stack-trellm"

log() {
  echo "[$(date -Iseconds)] [browser] $*"
}

mkdir -p "$PID_DIR" "$CHROME_USER_DATA"

start_xvfb() {
  if pgrep -f "Xvfb :${DISPLAY_NUM}" > /dev/null 2>&1; then
    log "Xvfb already running on :${DISPLAY_NUM}"
    return 0
  fi
  log "Starting Xvfb on :${DISPLAY_NUM}..."
  Xvfb ":${DISPLAY_NUM}" -screen 0 1920x1080x24 -ac &
  echo $! > "$PID_DIR/xvfb.pid"
  sleep 1
  if ! kill -0 "$(cat "$PID_DIR/xvfb.pid")" 2>/dev/null; then
    log "ERROR: Xvfb failed to start"
    return 1
  fi
  log "Xvfb started (PID $(cat "$PID_DIR/xvfb.pid"))"
}

start_chrome() {
  if pgrep -f "remote-debugging-port=${CDP_PORT}" > /dev/null 2>&1; then
    log "Chrome already running with CDP on port ${CDP_PORT}"
    return 0
  fi
  # Clear stale Singleton* lock files left by a previous Chrome instance
  # (e.g. when Chrome crashed or the profile was migrated). Without this
  # Chrome refuses to start with "profile in use by another Chrome process".
  rm -f "${CHROME_USER_DATA}/SingletonLock" \
        "${CHROME_USER_DATA}/SingletonCookie" \
        "${CHROME_USER_DATA}/SingletonSocket"
  log "Starting Chrome (headed, CDP port ${CDP_PORT})..."
  DISPLAY="${DISPLAY}" google-chrome \
    --remote-debugging-port="${CDP_PORT}" \
    --user-data-dir="${CHROME_USER_DATA}" \
    --no-first-run \
    --no-default-browser-check \
    --disable-background-timer-throttling \
    --disable-backgrounding-occluded-windows \
    --disable-renderer-backgrounding \
    --disable-dev-shm-usage \
    --window-size=1920,1080 \
    --start-maximized \
    "about:blank" &
  echo $! > "$PID_DIR/chrome.pid"
  sleep 2
  if ! pgrep -f "remote-debugging-port=${CDP_PORT}" > /dev/null 2>&1; then
    log "ERROR: Chrome failed to start"
    return 1
  fi
  log "Chrome started with CDP on port ${CDP_PORT}"
}

start_vnc() {
  if pgrep -f "x11vnc.*:${DISPLAY_NUM}" > /dev/null 2>&1; then
    log "x11vnc already running"
    return 0
  fi
  log "Starting x11vnc on port ${VNC_PORT}..."
  x11vnc -display ":${DISPLAY_NUM}" -rfbport "${VNC_PORT}" -nopw -forever -shared -bg \
    -o "$PID_DIR/x11vnc.log" 2>/dev/null
  sleep 1
  if ! pgrep -f "x11vnc.*:${DISPLAY_NUM}" > /dev/null 2>&1; then
    log "ERROR: x11vnc failed to start"
    return 1
  fi
  log "x11vnc started on port ${VNC_PORT}"
}

start_novnc() {
  if pgrep -f "websockify.*${NOVNC_PORT}" > /dev/null 2>&1; then
    log "noVNC already running on port ${NOVNC_PORT}"
    return 0
  fi
  log "Starting noVNC on port ${NOVNC_PORT}..."
  websockify --web /usr/share/novnc "${NOVNC_PORT}" "localhost:${VNC_PORT}" \
    > "$PID_DIR/novnc.log" 2>&1 &
  echo $! > "$PID_DIR/novnc.pid"
  sleep 1
  if ! kill -0 "$(cat "$PID_DIR/novnc.pid")" 2>/dev/null; then
    log "ERROR: noVNC failed to start"
    return 1
  fi
  log "noVNC started on port ${NOVNC_PORT}"
}

do_start() {
  start_xvfb || return 1
  start_chrome || return 1
  start_vnc || return 1
  start_novnc || return 1
  log ""
  log "Browser stack running:"
  log "  Chrome CDP:  http://localhost:${CDP_PORT}"
  log "  VNC:         vnc://localhost:${VNC_PORT} (no password)"
  log "  noVNC:       http://localhost:${NOVNC_PORT}/vnc.html"
  log ""
  log "From another device:"
  log "  noVNC web:   http://$(hostname):${NOVNC_PORT}/vnc.html"
  log "  VNC app:     $(hostname):${VNC_PORT} (no password)"
}

do_stop() {
  log "Stopping browser stack..."
  # noVNC
  if [ -f "$PID_DIR/novnc.pid" ]; then
    kill "$(cat "$PID_DIR/novnc.pid")" 2>/dev/null
    rm -f "$PID_DIR/novnc.pid"
  fi
  pkill -f "websockify.*${NOVNC_PORT}" 2>/dev/null
  # x11vnc
  pkill -f "x11vnc.*:${DISPLAY_NUM}" 2>/dev/null
  # Chrome
  pkill -f "remote-debugging-port=${CDP_PORT}" 2>/dev/null
  # Xvfb
  if [ -f "$PID_DIR/xvfb.pid" ]; then
    kill "$(cat "$PID_DIR/xvfb.pid")" 2>/dev/null
    rm -f "$PID_DIR/xvfb.pid"
  fi
  pkill -f "Xvfb :${DISPLAY_NUM}" 2>/dev/null
  log "Browser stack stopped."
}

do_status() {
  echo "Xvfb:   $(pgrep -f "Xvfb :${DISPLAY_NUM}" > /dev/null 2>&1 && echo "running" || echo "stopped")"
  echo "Chrome: $(pgrep -f "remote-debugging-port=${CDP_PORT}" > /dev/null 2>&1 && echo "running" || echo "stopped")"
  echo "x11vnc: $(pgrep -f "x11vnc.*:${DISPLAY_NUM}" > /dev/null 2>&1 && echo "running" || echo "stopped")"
  echo "noVNC:  $(pgrep -f "websockify.*${NOVNC_PORT}" > /dev/null 2>&1 && echo "running" || echo "stopped")"
}

case "${1:-start}" in
  start)   do_start ;;
  stop)    do_stop ;;
  status)  do_status ;;
  restart) do_stop; sleep 1; do_start ;;
  *)
    echo "Usage: $0 [start|stop|status|restart]"
    exit 1
    ;;
esac
