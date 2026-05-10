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


class TestStartScriptBrowserLifecycle:
    """M4 — start-trellm.sh should bring up the headed Chrome stack
    automatically when the YAML config has any browser flag enabled.

    Why: trellm runs as a host service; if it spawns `claude --chrome`
    against a non-running Chrome the binary errors out. Auto-starting
    keeps the operator from having to remember a manual step.

    How: the script gates on `Config.is_browser_required_anywhere()`
    via the venv's Python (the venv is already set up by this point in
    the script's flow), and calls `scripts/start-browser.sh start`
    before launching the polling loop. Idempotent — re-running the
    script is safe."""

    def test_script_invokes_start_browser_conditionally(self):
        content = START_SCRIPT.read_text()
        assert "scripts/start-browser.sh" in content

    def test_script_uses_is_browser_required_anywhere_helper(self):
        """The gate must go through the public Config accessor — not
        direct dataclass introspection — so the resolution rules stay
        in one place."""
        content = START_SCRIPT.read_text()
        assert "is_browser_required_anywhere" in content

    def test_script_starts_browser_before_running_trellm(self):
        """Order matters: stack must be up before the polling loop spawns
        any `claude --chrome` subprocess."""
        content = START_SCRIPT.read_text()
        browser_idx = content.find("scripts/start-browser.sh")
        run_idx = content.find("run_trellm")
        assert browser_idx >= 0 and run_idx >= 0
        assert browser_idx < run_idx, (
            "browser stack invocation must appear before the trellm run "
            "function definition/call site is reached"
        )

    def test_script_failsoft_if_browser_stack_fails(self):
        """A browser-stack failure should not abort trellm startup —
        cards that don't browse should still get processed. Document
        the soft-fail by either trapping the error or using `||`."""
        content = START_SCRIPT.read_text()
        # Either explicit `|| ` after the start-browser invocation, or a
        # documented soft-fail message in the gating block.
        assert "|| " in content or "Warning" in content or "warn" in content.lower()
