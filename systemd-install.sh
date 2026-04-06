#!/bin/bash
# Install or uninstall trellm as a systemd user service.
#
# Usage:
#   ./systemd-install.sh          # Install and enable
#   ./systemd-install.sh install   # Install and enable
#   ./systemd-install.sh uninstall # Stop, disable, and remove

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="trellm"
SERVICE_FILE="$HOME/.config/systemd/user/${SERVICE_NAME}.service"
TEMPLATE="$SCRIPT_DIR/trellm.service"
VENV_DIR="$SCRIPT_DIR/.venv"

do_install() {
    # Ensure venv exists and trellm is installed
    if [ ! -d "$VENV_DIR" ]; then
        echo "Creating virtual environment..."
        python3 -m venv "$VENV_DIR"
    fi

    echo "Installing trellm in editable mode..."
    "$VENV_DIR/bin/pip" install -q -e "$SCRIPT_DIR"

    # Verify the entrypoint exists
    if [ ! -x "$VENV_DIR/bin/trellm" ]; then
        echo "Error: $VENV_DIR/bin/trellm not found after install" >&2
        exit 1
    fi

    # Generate service file from template
    mkdir -p "$(dirname "$SERVICE_FILE")"
    sed -e "s|TRELLM_VENV|$VENV_DIR|g" \
        -e "s|TRELLM_DIR|$SCRIPT_DIR|g" \
        "$TEMPLATE" > "$SERVICE_FILE"

    echo "Installed $SERVICE_FILE"

    # Enable lingering so user services start at boot (not just at login)
    if ! loginctl show-user "$USER" --property=Linger 2>/dev/null | grep -q "yes"; then
        echo "Enabling lingering for $USER (allows service to start at boot)..."
        loginctl enable-linger "$USER"
    fi

    # Reload and enable
    systemctl --user daemon-reload
    systemctl --user enable "$SERVICE_NAME"
    systemctl --user start "$SERVICE_NAME"

    echo ""
    echo "trellm service installed and started."
    echo ""
    echo "Useful commands:"
    echo "  systemctl --user status trellm    # Check status"
    echo "  journalctl --user -u trellm -f    # Follow logs"
    echo "  systemctl --user restart trellm   # Restart"
    echo "  systemctl --user stop trellm      # Stop"
    echo "  ./systemd-install.sh uninstall    # Remove service"
}

do_uninstall() {
    echo "Stopping and disabling trellm service..."
    systemctl --user stop "$SERVICE_NAME" 2>/dev/null || true
    systemctl --user disable "$SERVICE_NAME" 2>/dev/null || true

    if [ -f "$SERVICE_FILE" ]; then
        rm "$SERVICE_FILE"
        echo "Removed $SERVICE_FILE"
    fi

    systemctl --user daemon-reload
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
