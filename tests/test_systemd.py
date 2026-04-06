"""Tests for systemd service configuration."""

import os
import subprocess
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).parent.parent
SERVICE_TEMPLATE = PROJECT_DIR / "trellm.service"
INSTALL_SCRIPT = PROJECT_DIR / "systemd-install.sh"


class TestServiceTemplate:
    """Tests for the systemd service unit template."""

    def test_template_exists(self):
        assert SERVICE_TEMPLATE.exists()

    def test_template_has_required_sections(self):
        content = SERVICE_TEMPLATE.read_text()
        assert "[Unit]" in content
        assert "[Service]" in content
        assert "[Install]" in content

    def test_template_has_placeholders(self):
        content = SERVICE_TEMPLATE.read_text()
        assert "TRELLM_VENV" in content
        assert "TRELLM_DIR" in content

    def test_template_exec_start_uses_venv_binary(self):
        content = SERVICE_TEMPLATE.read_text()
        assert "ExecStart=TRELLM_VENV/bin/trellm" in content

    def test_template_restarts_on_failure(self):
        content = SERVICE_TEMPLATE.read_text()
        assert "Restart=on-failure" in content

    def test_template_waits_for_network(self):
        content = SERVICE_TEMPLATE.read_text()
        assert "After=network-online.target" in content

    def test_template_targets_user_default(self):
        content = SERVICE_TEMPLATE.read_text()
        assert "WantedBy=default.target" in content

    def test_template_sets_path(self):
        content = SERVICE_TEMPLATE.read_text()
        assert "Environment=PATH=" in content

    def test_placeholder_substitution(self):
        """Verify sed substitution produces a valid service file."""
        content = SERVICE_TEMPLATE.read_text()
        result = content.replace("TRELLM_VENV", "/home/user/src/trellm/.venv")
        result = result.replace("TRELLM_DIR", "/home/user/src/trellm")
        assert "ExecStart=/home/user/src/trellm/.venv/bin/trellm" in result
        assert "WorkingDirectory=/home/user/src/trellm" in result
        # No remaining placeholders
        assert "TRELLM_VENV" not in result
        assert "TRELLM_DIR" not in result


class TestInstallScript:
    """Tests for the systemd install script."""

    def test_script_exists(self):
        assert INSTALL_SCRIPT.exists()

    def test_script_is_executable(self):
        assert os.access(INSTALL_SCRIPT, os.X_OK)

    def test_script_has_bash_shebang(self):
        content = INSTALL_SCRIPT.read_text()
        assert content.startswith("#!/bin/bash")

    def test_script_uses_strict_mode(self):
        content = INSTALL_SCRIPT.read_text()
        assert "set -euo pipefail" in content

    def test_script_supports_install_command(self):
        content = INSTALL_SCRIPT.read_text()
        assert "do_install" in content

    def test_script_supports_uninstall_command(self):
        content = INSTALL_SCRIPT.read_text()
        assert "do_uninstall" in content

    def test_script_enables_linger(self):
        """Service should enable lingering for boot-time start."""
        content = INSTALL_SCRIPT.read_text()
        assert "enable-linger" in content

    def test_script_installs_to_user_systemd_dir(self):
        content = INSTALL_SCRIPT.read_text()
        assert ".config/systemd/user" in content

    def test_script_invalid_command_exits_nonzero(self):
        result = subprocess.run(
            [str(INSTALL_SCRIPT), "invalid"],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "Usage:" in result.stderr
