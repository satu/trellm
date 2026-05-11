"""Tests for the M3 auto-start path: scripts/needs-browser-stack.py and
the additions to start-trellm.sh that invoke it.

These tests do not actually start Chrome — they isolate the decision
logic (which is the only thing M3 introduces) by either calling
scripts/needs-browser-stack.py directly with a fixture YAML, or by
static-checking start-trellm.sh for the structural invariants the
auto-start path depends on.

Live runtime verification is documented in docs/patchright-mcp.md §6/M3
and is run manually after each commit that touches start-trellm.sh.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
NEEDS_BROWSER_SCRIPT = REPO_ROOT / "scripts" / "needs-browser-stack.py"
START_TRELLM_SCRIPT = REPO_ROOT / "start-trellm.sh"

# Minimal valid trello block — load_config accepts empty strings so we
# only need the keys to exist.
_MIN_TRELLO = {
    "api_key": "k", "api_token": "t",
    "board_id": "b", "todo_list_id": "l",
}


def _write_config(tmp_path: Path, claude_block: dict) -> Path:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump({"trello": _MIN_TRELLO, "claude": claude_block}))
    return cfg_path


def _run_needs_browser(cfg_path: Path) -> int:
    """Invoke scripts/needs-browser-stack.py against `cfg_path` and return
    the exit code. 0 = browser stack needed; 1 = not needed; >1 = error."""
    result = subprocess.run(
        [sys.executable, str(NEEDS_BROWSER_SCRIPT), str(cfg_path)],
        capture_output=True,
        text=True,
    )
    return result.returncode


class TestNeedsBrowserStackScript:
    """The decision-logic helper that start-trellm.sh calls to figure out
    whether to bring up the browser stack before launching trellm."""

    def test_script_exists(self):
        assert NEEDS_BROWSER_SCRIPT.exists(), f"missing: {NEEDS_BROWSER_SCRIPT}"

    def test_script_is_executable(self):
        assert os.access(NEEDS_BROWSER_SCRIPT, os.X_OK)

    def test_script_has_python_shebang(self):
        first_line = NEEDS_BROWSER_SCRIPT.read_text().splitlines()[0]
        assert first_line.startswith("#!"), "must have a shebang line"
        assert "python" in first_line, "must invoke python"

    def test_no_browser_block_anywhere_yields_exit_1(self, tmp_path):
        """The default case for every existing trellm install — no browser
        block at all. Must exit 1 (no auto-start needed) so we don't
        regress behaviour for everyone who hasn't opted in."""
        cfg = _write_config(tmp_path, {"projects": {"p1": {"working_dir": "/tmp/p1"}}})
        assert _run_needs_browser(cfg) == 1

    def test_global_browser_enabled_yields_exit_0(self, tmp_path):
        cfg = _write_config(
            tmp_path,
            {
                "browser": {"enabled": True},
                "projects": {"p1": {"working_dir": "/tmp/p1"}},
            },
        )
        assert _run_needs_browser(cfg) == 0

    def test_one_project_browser_enabled_yields_exit_0(self, tmp_path):
        """Any single project flipped on should trigger auto-start —
        we can't easily tell from start-trellm.sh which project's card
        will arrive first, so we err on the side of starting the stack."""
        cfg = _write_config(
            tmp_path,
            {
                "projects": {
                    "p1": {"working_dir": "/tmp/p1"},
                    "p2": {
                        "working_dir": "/tmp/p2",
                        "browser": {"enabled": True},
                    },
                    "p3": {"working_dir": "/tmp/p3"},
                },
            },
        )
        assert _run_needs_browser(cfg) == 0

    def test_global_off_with_all_projects_off_yields_exit_1(self, tmp_path):
        """Explicit `enabled: false` everywhere → exit 1. Helps verify
        that an explicit-off doesn't accidentally trigger auto-start."""
        cfg = _write_config(
            tmp_path,
            {
                "browser": {"enabled": False},
                "projects": {
                    "p1": {
                        "working_dir": "/tmp/p1",
                        "browser": {"enabled": False},
                    },
                },
            },
        )
        assert _run_needs_browser(cfg) == 1

    def test_global_on_but_only_project_explicitly_off_still_yields_exit_0(
        self, tmp_path
    ):
        """If the global is on, even a single project overriding off doesn't
        change the decision — the global setting alone is enough reason
        to bring up the stack (other projects might inherit the global,
        and the auto-start has to cover the most-permissive case)."""
        cfg = _write_config(
            tmp_path,
            {
                "browser": {"enabled": True},
                "projects": {
                    "p1": {
                        "working_dir": "/tmp/p1",
                        "browser": {"enabled": False},
                    },
                },
            },
        )
        assert _run_needs_browser(cfg) == 0

    def test_missing_config_file_yields_exit_1_not_crash(self, tmp_path):
        """Helper must be robust to a non-existent config path so
        start-trellm.sh doesn't blow up on a fresh install. No browser
        block discoverable → no auto-start, same as 'not needed'."""
        nonexistent = tmp_path / "does-not-exist.yaml"
        assert not nonexistent.exists()
        # load_config silently treats missing file as empty dict, so the
        # decision falls through to "no browser config" → exit 1.
        assert _run_needs_browser(nonexistent) == 1


class TestStartTrellmAutoStart:
    """Static structural checks on start-trellm.sh — we cannot exercise
    the actual launch from a unit test (it would fork trellm and a real
    Chrome). These assertions guarantee the auto-start invariants we
    care about can't silently regress."""

    def test_start_trellm_calls_needs_browser_helper(self):
        content = START_TRELLM_SCRIPT.read_text()
        assert "needs-browser-stack.py" in content, (
            "start-trellm.sh must invoke scripts/needs-browser-stack.py"
        )

    def test_start_trellm_invokes_start_browser_on_demand(self):
        """When the helper says browser is needed, start-trellm.sh must
        invoke scripts/start-browser.sh — otherwise the auto-start path
        is dead code."""
        content = START_TRELLM_SCRIPT.read_text()
        assert "start-browser.sh" in content

    def test_start_trellm_waits_for_cdp(self):
        """After invoking the browser stack, start-trellm.sh must verify
        CDP comes up on 9222 before launching trellm. The auto-start path
        should fail loudly rather than let trellm run browser-enabled
        cards against a dead browser."""
        content = START_TRELLM_SCRIPT.read_text()
        # Either curl probes 9222, or we shell-out to scripts/start-browser.sh status
        assert "9222" in content, (
            "start-trellm.sh must verify CDP is reachable on 9222 before launching trellm"
        )

    def test_start_trellm_does_not_stop_browser_on_exit(self):
        """The browser stack is host-owned and persists across trellm
        restarts (so VNC stays usable, the profile keeps cookies, and a
        trellm restart doesn't pay Chrome cold-start). Verify we never
        call `start-browser.sh stop` — that would be an immediate
        lifetime bug."""
        content = START_TRELLM_SCRIPT.read_text()
        assert "start-browser.sh stop" not in content
        assert "start-browser.sh restart" not in content

    def test_start_trellm_passes_bash_syntax(self):
        result = subprocess.run(
            ["bash", "-n", str(START_TRELLM_SCRIPT)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"bash -n failed: {result.stderr}"
