#!/bin/bash
# Install or uninstall trellm as a systemd service.
#
# Usage:
#   sudo ./systemd-install.sh          # Install and enable
#   sudo ./systemd-install.sh install   # Install and enable
#   sudo ./systemd-install.sh uninstall # Remove service

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="trellm"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
TEMPLATE="$SCRIPT_DIR/trellm.service"
VENV_DIR="$SCRIPT_DIR/.venv"

# Determine the user who owns the project directory (handles both sudo and direct run)
TRELLM_USER="$(stat -c '%U' "$SCRIPT_DIR")"
TRELLM_HOME="$(eval echo "~$TRELLM_USER")"

do_install() {
    # Ensure venv exists and trellm is installed
    if [ ! -d "$VENV_DIR" ]; then
        echo "Creating virtual environment..."
        sudo -u "$TRELLM_USER" python3 -m venv "$VENV_DIR"
    fi

    echo "Installing trellm in editable mode..."
    sudo -u "$TRELLM_USER" "$VENV_DIR/bin/pip" install -q -e "$SCRIPT_DIR"

    # Verify the entrypoint exists
    if [ ! -x "$VENV_DIR/bin/trellm" ]; then
        echo "Error: $VENV_DIR/bin/trellm not found after install" >&2
        exit 1
    fi

    # Generate service file from template
    sed -e "s|TRELLM_VENV|$VENV_DIR|g" \
        -e "s|TRELLM_DIR|$SCRIPT_DIR|g" \
        -e "s|TRELLM_USER|$TRELLM_USER|g" \
        -e "s|TRELLM_HOME|$TRELLM_HOME|g" \
        "$TEMPLATE" > "$SERVICE_FILE"

    echo "Installed $SERVICE_FILE"

    # Check if systemd is running
    if ! systemctl --no-pager status >/dev/null 2>&1; then
        echo ""
        echo "Service file installed, but systemd is not available."
        echo "On a systemd-based system, run:"
        echo "  sudo systemctl daemon-reload"
        echo "  sudo systemctl enable --now trellm"
        return
    fi

    # Reload and enable
    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
    systemctl start "$SERVICE_NAME"

    echo ""
    echo "trellm service installed and started."
    echo ""
    echo "Useful commands:"
    echo "  systemctl status trellm         # Check status"
    echo "  journalctl -u trellm -f         # Follow logs"
    echo "  sudo systemctl restart trellm   # Restart"
    echo "  sudo systemctl stop trellm      # Stop"
    echo "  sudo ./systemd-install.sh uninstall  # Remove service"
}

do_uninstall() {
    echo "Stopping and disabling trellm service..."
    systemctl stop "$SERVICE_NAME" 2>/dev/null || true
    systemctl disable "$SERVICE_NAME" 2>/dev/null || true

    if [ -f "$SERVICE_FILE" ]; then
        rm "$SERVICE_FILE"
        echo "Removed $SERVICE_FILE"
    fi

    systemctl daemon-reload
    echo "trellm service uninstalled."
}

case "${1:-install}" in
    install)
        do_install
        ;;
    uninstall)
        do_uninstall
        ;;
    *)
        echo "Usage: $0 [install|uninstall]" >&2
        exit 1
        ;;
esac
