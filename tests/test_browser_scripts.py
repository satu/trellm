"""Tests for the browser-stack shell scripts (Xvfb + Chrome + x11vnc + noVNC).

These tests exercise the static structure of the scripts — they do not
actually start the stack, since CI typically lacks a usable display. Live
verification is documented in docs/patchright-mcp.md (§6 / M1) and is run
manually after each commit that touches the scripts.

The browser stack here is the trellm-side host-mode Chrome that the
patchright-mcp-lite MCP server attaches to via CDP on localhost:9222.
There is no Web-Store claude-in-chrome extension involved — that route
was reverted in f0ccda9 and the present plan is documented in
docs/patchright-mcp.md.
"""

import os
import subprocess
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
SETUP_SCRIPT = SCRIPTS_DIR / "setup-browser.sh"
START_SCRIPT = SCRIPTS_DIR / "start-browser.sh"

CLAUDE_IN_CHROME_EXTENSION_ID = "fcoeoabgfenejglbffodgkkbkcdhcgfn"


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
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"bash -n failed: {result.stderr}"

    def test_script_installs_required_packages(self):
        content = SETUP_SCRIPT.read_text()
        for pkg in ("xvfb", "x11vnc", "novnc", "websockify"):
            assert pkg in content, f"setup script must install {pkg}"

    def test_script_does_not_install_claude_in_chrome_extension(self):
        """The patchright-mcp-lite route replaces the claude-in-chrome
        extension. Setup must not reference the reverted extension ID."""
        content = SETUP_SCRIPT.read_text()
        assert CLAUDE_IN_CHROME_EXTENSION_ID not in content
        assert "claude-in-chrome" not in content


class TestStartBrowserScript:
    """Static checks on scripts/start-browser.sh (start | stop | status | restart)."""

    def test_script_exists(self):
        assert START_SCRIPT.exists(), f"missing: {START_SCRIPT}"

    def test_script_is_executable(self):
        assert os.access(START_SCRIPT, os.X_OK)

    def test_script_has_bash_shebang(self):
        assert START_SCRIPT.read_text().startswith("#!/usr/bin/env bash")

    def test_script_uses_strict_mode(self):
        # We don't require -e here: a single failing pgrep shouldn't kill
        # the whole script. -uo pipefail matches humphrey's equivalent.
        content = START_SCRIPT.read_text()
        assert "set -uo pipefail" in content or "set -euo pipefail" in content

    def test_script_passes_bash_syntax_check(self):
        result = subprocess.run(
            ["bash", "-n", str(START_SCRIPT)],
            capture_output=True,
            text=True,
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
        # CDP for patchright-mcp-lite to attach via chromium.connectOverCDP
        assert "9222" in content
        # VNC + noVNC for remote viewing — re-using humphrey's freed ports
        # since humphrey now runs in its own Docker container.
        assert "5900" in content
        assert "6080" in content

    def test_script_starts_full_stack(self):
        content = START_SCRIPT.read_text()
        for component in ("Xvfb", "google-chrome", "x11vnc", "websockify"):
            assert component in content, f"start path missing: {component}"

    def test_script_clears_singleton_locks(self):
        """Without this, Chrome refuses to start when a SingletonLock is left
        over from a prior crashed instance."""
        content = START_SCRIPT.read_text()
        for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            assert lock in content, f"start path must clear {lock}"

    def test_script_supports_lifecycle_subcommands(self):
        content = START_SCRIPT.read_text()
        for cmd in ("start", "stop", "status", "restart"):
            assert f"{cmd})" in content, f"missing case branch: {cmd})"

    def test_script_does_not_symlink_claude_in_chrome_extension(self):
        """The patchright-mcp-lite route is the replacement for the Web-Store
        claude-in-chrome extension. The start path must not create the
        ~/.config/google-chrome/Default/Extensions/<EXTID> symlink — that
        was the disambiguation knob the cloud bridge ignored, and pointing
        Chrome at the extension dir again would re-introduce the same
        cross-stack pairing problem with humphrey."""
        content = START_SCRIPT.read_text()
        assert CLAUDE_IN_CHROME_EXTENSION_ID not in content
        assert "claude-in-chrome" not in content
        # Defensive: the canonical extension scan path should not appear as
        # a symlink target either.
        assert ".config/google-chrome/Default/Extensions" not in content

    def test_script_does_not_use_no_sandbox(self):
        """We run as a non-root user; Chrome's sandbox works fine without
        --no-sandbox on modern Ubuntu. Dropping it tightens isolation of
        the browser against any compromised renderer. If a future change
        re-introduces it, this test forces a deliberate update and a
        documented reason."""
        content = START_SCRIPT.read_text()
        assert "--no-sandbox" not in content
