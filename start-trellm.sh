#!/bin/bash
# Start trellm as a long-running service with verbose logging.
# Designed to be called from a container entrypoint.
#
# Logs go to /var/log/trellm.log (stdout+stderr combined).
# The process runs in the background; use the PID file to manage it.
#
# Usage:
#   ./start-trellm.sh          # Start in background
#   ./start-trellm.sh --fg     # Start in foreground (for debugging)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
LOG_FILE="/var/log/trellm.log"
PID_FILE="/var/run/trellm.pid"

# Ensure venv exists and trellm is installed
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/pip" install -q -e "$SCRIPT_DIR"

if [ ! -x "$VENV_DIR/bin/trellm" ]; then
    echo "Error: $VENV_DIR/bin/trellm not found after install" >&2
    exit 1
fi

# If the YAML config enables browsing for the global block or any project,
# bring up the headed Chrome stack (Xvfb + Chrome + x11vnc + noVNC) before
# the polling loop starts spawning `claude --chrome`. The stack runs as
# host-level processes — see scripts/start-browser.sh for the lifecycle.
# Soft-fail: a browser-stack failure must not block trellm startup, since
# non-browsing cards should still process. Stack restart on Chrome crash
# is NOT yet handled — operator-driven via `bash scripts/start-browser.sh
# restart`.
if "$VENV_DIR/bin/python" -c "import sys; from trellm.config import load_config; sys.exit(0 if load_config().is_browser_required_anywhere() else 1)" 2>/dev/null; then
    echo "Browser config enabled — starting Chrome stack..."
    bash "$SCRIPT_DIR/scripts/start-browser.sh" start \
        || echo "Warning: browser stack failed to start, continuing anyway. Browsing cards will fail until you fix it."
fi

run_trellm() {
    exec "$VENV_DIR/bin/trellm" -v 2>&1
}

if [ "${1:-}" = "--fg" ]; then
    echo "Starting trellm in foreground (verbose)..."
    run_trellm | tee -a "$LOG_FILE"
else
    echo "Starting trellm (logging to $LOG_FILE)..."
    run_trellm >> "$LOG_FILE" &
    echo $! > "$PID_FILE"
    echo "trellm started (PID $(cat "$PID_FILE"))"
fi
