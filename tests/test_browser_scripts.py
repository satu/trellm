"""Tests for the browser-stack shell scripts (Xvfb + Chrome + x11vnc + noVNC).

These tests exercise the static structure of the scripts (existence, shebang,
strict-mode flags, syntax) — they do not actually start the stack, since CI
typically lacks a usable display. Live verification is documented in the
Trello card and is run manually after each commit that touches the scripts.
"""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
SETUP_SCRIPT = SCRIPTS_DIR / "setup-browser.sh"
START_SCRIPT = SCRIPTS_DIR / "start-browser.sh"


class TestSetupBrowserScript:
    """Static checks on scripts/setup-browser.sh (one-shot host-tooling installer)."""

    def test_script_exists(self):
        assert SETUP_SCRIPT.exists(), f"missing: {SETUP_SCRIPT}"

    def test_script_is_executable(self):
        assert os.access(SETUP_SCRIPT, os.X_OK)

    def test_script_has_bash_shebang(self):
        assert SETUP_SCRIPT.read_text().startswith("#!/usr/bin/env bash")

    def test_script_uses_strict_mode(self):
        assert "set -euo pipefail" in SETUP_SCRIPT.read_text()

    def test_script_passes_bash_syntax_check(self):
        result = subprocess.run(
            ["bash", "-n", str(SETUP_SCRIPT)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"bash -n failed: {result.stderr}"

    def test_script_installs_required_packages(self):
        content = SETUP_SCRIPT.read_text()
        # Display stack: virtual display + remote viewing
        for pkg in ("xvfb", "x11vnc", "novnc", "websockify"):
            assert pkg in content, f"setup script must install {pkg}"


class TestStartBrowserScript:
    """Static checks on scripts/start-browser.sh (start | stop | status | restart)."""

    def test_script_exists(self):
        assert START_SCRIPT.exists(), f"missing: {START_SCRIPT}"

    def test_script_is_executable(self):
        assert os.access(START_SCRIPT, os.X_OK)

    def test_script_has_bash_shebang(self):
        assert START_SCRIPT.read_text().startswith("#!/usr/bin/env bash")

    def test_script_uses_strict_mode(self):
        # We don't require -e here: humphrey's equivalent uses `set -uo pipefail`
        # so a single failing pgrep doesn't kill the whole script. Match that.
        content = START_SCRIPT.read_text()
        assert "set -uo pipefail" in content or "set -euo pipefail" in content

    def test_script_passes_bash_syntax_check(self):
        result = subprocess.run(
            ["bash", "-n", str(START_SCRIPT)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"bash -n failed: {result.stderr}"

    def test_script_uses_dedicated_trellm_chrome_profile(self):
        """The Chrome profile dir must be ~/.chrome-trellm — distinct from
        humphrey's ~/.chrome-humphrey so the two stacks never collide on the
        same host even if humphrey re-migrates to host-mode someday."""
        content = START_SCRIPT.read_text()
        assert ".chrome-trellm" in content
        assert ".chrome-humphrey" not in content

    def test_script_uses_canonical_ports(self):
        content = START_SCRIPT.read_text()
        # CDP for the Claude extension to attach via --chrome
        assert "9222" in content
        # VNC + noVNC for remote viewing — re-using humphrey's freed ports
        # since humphrey now runs in its own Docker container.
        assert "5900" in content
        assert "6080" in content

    def test_script_starts_full_stack(self):
        content = START_SCRIPT.read_text()
        # Each component the start path must launch
        assert "Xvfb" in content
        assert "google-chrome" in content
        assert "x11vnc" in content
        assert "websockify" in content

    def test_script_clears_singleton_locks(self):
        """Without this, Chrome refuses to start when a SingletonLock is left
        over from a prior crashed instance — same gotcha humphrey hit when the
        profile was migrated across hosts."""
        content = START_SCRIPT.read_text()
        assert "SingletonLock" in content
        assert "SingletonCookie" in content
        assert "SingletonSocket" in content

    def test_script_symlinks_extension_dir(self):
        """The Claude CLI's --chrome flag scans
        ~/.config/google-chrome/Default/Extensions/ for the
        claude-in-chrome extension. Our profile lives elsewhere, so the
        start path must symlink the extension dir into the canonical path
        on every start. Without this symlink, --chrome can't find the
        extension and browsing tools won't attach."""
        content = START_SCRIPT.read_text()
        assert "fcoeoabgfenejglbffodgkkbkcdhcgfn" in content
        assert ".config/google-chrome/Default/Extensions" in content
        # ln with -f (force replace) so re-runs don't error on existing symlink
        assert "ln -sfn" in content or "ln -fsn" in content

    def test_script_supports_lifecycle_subcommands(self):
        content = START_SCRIPT.read_text()
        for cmd in ("start", "stop", "status", "restart"):
            assert f"{cmd})" in content, f"missing case branch: {cmd})"
