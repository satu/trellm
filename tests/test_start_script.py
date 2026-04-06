"""Tests for the start-trellm.sh startup script."""

import os
import subprocess
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).parent.parent
START_SCRIPT = PROJECT_DIR / "start-trellm.sh"


class TestStartScript:
    """Tests for the start-trellm.sh script."""

    def test_script_exists(self):
        assert START_SCRIPT.exists()

    def test_script_is_executable(self):
        assert os.access(START_SCRIPT, os.X_OK)

    def test_script_has_bash_shebang(self):
        content = START_SCRIPT.read_text()
        assert content.startswith("#!/bin/bash")

    def test_script_uses_strict_mode(self):
        content = START_SCRIPT.read_text()
        assert "set -euo pipefail" in content

    def test_script_uses_verbose_flag(self):
        content = START_SCRIPT.read_text()
        assert 'trellm" -v' in content

    def test_script_logs_to_file(self):
        content = START_SCRIPT.read_text()
        assert "/var/log/trellm.log" in content

    def test_script_has_pid_file(self):
        content = START_SCRIPT.read_text()
        assert "/var/run/trellm.pid" in content

    def test_script_supports_foreground_mode(self):
        content = START_SCRIPT.read_text()
        assert "--fg" in content

    def test_script_creates_venv_if_missing(self):
        content = START_SCRIPT.read_text()
        assert "python3 -m venv" in content

    def test_script_installs_trellm(self):
        content = START_SCRIPT.read_text()
        assert "pip" in content and "install" in content
        assert "-e" in content

    def test_script_verifies_entrypoint(self):
        """Script should check that trellm binary exists after install."""
        content = START_SCRIPT.read_text()
        assert "bin/trellm" in content
        assert "not found" in content
