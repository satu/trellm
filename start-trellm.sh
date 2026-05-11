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

# Auto-start the patchright browser stack when any project (or the
# global default) opts in via claude.browser.enabled in the trellm
# config. We do NOT stop the stack on exit — it's a host-owned process
# so VNC stays usable, the ~/.chrome-trellm profile keeps cookies, and
# subsequent trellm restarts don't pay Chrome's cold-start cost.
maybe_start_browser_stack() {
    if "$VENV_DIR/bin/python" "$SCRIPT_DIR/scripts/needs-browser-stack.py"; then
        echo "Browser stack required by config; bringing it up..."
        if ! "$SCRIPT_DIR/scripts/start-browser.sh" start; then
            echo "ERROR: scripts/start-browser.sh start failed." >&2
            echo "       Run scripts/setup-browser.sh first if you haven't." >&2
            exit 1
        fi
        # Wait up to 10s for CDP to come up on 9222. Fail loudly if it
        # doesn't — we must not let trellm run browser-enabled cards
        # against a dead browser (the spawned claude subprocess would
        # hang every time it tried to use the patchright MCP).
        local i
        for i in 1 2 3 4 5 6 7 8 9 10; do
            if curl -sf -o /dev/null --max-time 1 \
                http://localhost:9222/json/version; then
                echo "Chrome CDP reachable on http://localhost:9222"
                return 0
            fi
            sleep 1
        done
        echo "ERROR: CDP did not become reachable on localhost:9222 within 10s." >&2
        echo "       Check scripts/start-browser.sh status and the browser logs" >&2
        echo "       at ~/.browser-stack-trellm/. Aborting trellm startup." >&2
        exit 1
    fi
}

maybe_start_browser_stack

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
