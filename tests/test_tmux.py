"""Tests for the tmux control module (trellm/tmux.py).

docs/claude-interactive.md M2 adds a low-level async wrapper around the
`tmux` binary — the foundation the interactive `claude` transport (M4)
builds on. M2 is the wrapper and *only* the wrapper: no `claude` yet.

These tests pin two things:

  * Every method shells out to exactly the right `tmux` argv. The argv is
    load-bearing — M4 dispatches prompts by typing them into a window, so
    a wrong flag is a silent failure. Unit tests run against a mocked
    `tmux` (patched `asyncio.create_subprocess_exec`).
  * One integration test drives a real `tmux` end to end, skipped when
    `tmux` is not installed.
"""

import asyncio
import os
import shutil
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trellm.tmux import DEFAULT_SESSION, TmuxController, TmuxError


def _mock_proc(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> MagicMock:
    """A subprocess stand-in: `.returncode` plus an awaitable `.communicate`."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


def _argv(mock_exec: MagicMock, call_index: int = 0) -> list:
    """The tmux argv (positional args) of the Nth create_subprocess_exec call."""
    return list(mock_exec.call_args_list[call_index].args)


class TestTmuxArgvConstruction:
    """Each method must build the exact tmux command line documented in
    docs/claude-interactive.md §6.1 / §8."""

    @pytest.mark.asyncio
    async def test_has_session_true_on_zero_exit(self):
        controller = TmuxController()
        with patch(
            "asyncio.create_subprocess_exec", return_value=_mock_proc(returncode=0)
        ) as mock_exec:
            assert await controller.has_session() is True
        assert _argv(mock_exec) == [
            "tmux", "has-session", "-t", "trellm-interactive",
        ]

    @pytest.mark.asyncio
    async def test_has_session_false_on_nonzero_exit(self):
        """has-session exits non-zero when the session is absent — that is
        the answer, not an error, so it must not raise."""
        controller = TmuxController()
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=_mock_proc(returncode=1, stderr=b"no server running"),
        ):
            assert await controller.has_session() is False

    @pytest.mark.asyncio
    async def test_create_session_creates_when_absent(self):
        """Absent session → new-session runs, returns True."""
        controller = TmuxController()
        procs = [_mock_proc(returncode=1), _mock_proc(returncode=0)]
        with patch(
            "asyncio.create_subprocess_exec", side_effect=procs
        ) as mock_exec:
            created = await controller.create_session()
        assert created is True
        assert _argv(mock_exec, 0)[:2] == ["tmux", "has-session"]
        assert _argv(mock_exec, 1) == [
            "tmux", "new-session", "-d", "-s", "trellm-interactive",
        ]

    @pytest.mark.asyncio
    async def test_create_session_noop_when_present(self):
        """Existing session → new-session is NOT run, returns False."""
        controller = TmuxController()
        with patch(
            "asyncio.create_subprocess_exec", return_value=_mock_proc(returncode=0)
        ) as mock_exec:
            created = await controller.create_session()
        assert created is False
        # Only the has-session probe ran.
        assert mock_exec.call_count == 1
        assert _argv(mock_exec, 0)[:2] == ["tmux", "has-session"]

    @pytest.mark.asyncio
    async def test_create_window_without_command(self, tmp_path):
        controller = TmuxController()
        with patch(
            "asyncio.create_subprocess_exec", return_value=_mock_proc()
        ) as mock_exec:
            await controller.create_window("demo", str(tmp_path))
        assert _argv(mock_exec) == [
            "tmux", "new-window", "-d", "-t", "trellm-interactive",
            "-n", "demo", "-c", str(tmp_path),
        ]

    @pytest.mark.asyncio
    async def test_create_window_with_command_appends_it_last(self, tmp_path):
        """A command, when given, is the final positional arg — M4 passes
        the `claude` invocation here."""
        controller = TmuxController()
        with patch(
            "asyncio.create_subprocess_exec", return_value=_mock_proc()
        ) as mock_exec:
            await controller.create_window(
                "demo", str(tmp_path), command="claude --continue"
            )
        assert _argv(mock_exec) == [
            "tmux", "new-window", "-d", "-t", "trellm-interactive",
            "-n", "demo", "-c", str(tmp_path), "claude --continue",
        ]

    @pytest.mark.asyncio
    async def test_create_window_expands_user_in_working_dir(self):
        """`~` in working_dir is expanded, mirroring claude.py / maintenance.py."""
        controller = TmuxController()
        with patch(
            "asyncio.create_subprocess_exec", return_value=_mock_proc()
        ) as mock_exec:
            await controller.create_window("demo", "~/src/demo")
        cwd = _argv(mock_exec)[_argv(mock_exec).index("-c") + 1]
        assert cwd == os.path.expanduser("~/src/demo")
        assert "~" not in cwd

    @pytest.mark.asyncio
    async def test_list_windows_parses_names(self):
        controller = TmuxController()
        procs = [
            _mock_proc(returncode=0),  # has-session
            _mock_proc(returncode=0, stdout=b"trellm\njcapp\nsmugcoin\n"),
        ]
        with patch(
            "asyncio.create_subprocess_exec", side_effect=procs
        ) as mock_exec:
            windows = await controller.list_windows()
        assert windows == ["trellm", "jcapp", "smugcoin"]
        assert _argv(mock_exec, 1) == [
            "tmux", "list-windows", "-t", "trellm-interactive",
            "-F", "#{window_name}",
        ]

    @pytest.mark.asyncio
    async def test_list_windows_empty_when_session_absent(self):
        """A cold start has no session — list_windows returns [] rather than
        raising, which is what doc §6.1's restart path relies on."""
        controller = TmuxController()
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=_mock_proc(returncode=1),
        ) as mock_exec:
            windows = await controller.list_windows()
        assert windows == []
        # Only the has-session probe ran; list-windows was skipped.
        assert mock_exec.call_count == 1

    @pytest.mark.asyncio
    async def test_send_keys_single_key(self):
        controller = TmuxController()
        with patch(
            "asyncio.create_subprocess_exec", return_value=_mock_proc()
        ) as mock_exec:
            await controller.send_keys("demo", "Enter")
        assert _argv(mock_exec) == [
            "tmux", "send-keys", "-t", "trellm-interactive:demo", "Enter",
        ]

    @pytest.mark.asyncio
    async def test_send_keys_multiple_keys(self):
        controller = TmuxController()
        with patch(
            "asyncio.create_subprocess_exec", return_value=_mock_proc()
        ) as mock_exec:
            await controller.send_keys("demo", "Escape", "Enter")
        assert _argv(mock_exec) == [
            "tmux", "send-keys", "-t", "trellm-interactive:demo",
            "Escape", "Enter",
        ]

    @pytest.mark.asyncio
    async def test_send_keys_requires_at_least_one_key(self):
        controller = TmuxController()
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            with pytest.raises(ValueError, match="at least one key"):
                await controller.send_keys("demo")
        mock_exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_literal_uses_dash_l(self):
        """Literal text goes through `send-keys -l` so spaces and words like
        `Enter` are typed as characters, not interpreted as key names."""
        controller = TmuxController()
        with patch(
            "asyncio.create_subprocess_exec", return_value=_mock_proc()
        ) as mock_exec:
            await controller.send_literal("demo", "Read the task and Enter it")
        assert _argv(mock_exec) == [
            "tmux", "send-keys", "-t", "trellm-interactive:demo",
            "-l", "Read the task and Enter it",
        ]

    @pytest.mark.asyncio
    async def test_capture_pane_returns_stdout(self):
        controller = TmuxController()
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=_mock_proc(stdout=b"pane line one\npane line two\n"),
        ) as mock_exec:
            text = await controller.capture_pane("demo")
        assert text == "pane line one\npane line two\n"
        assert _argv(mock_exec) == [
            "tmux", "capture-pane", "-p", "-t", "trellm-interactive:demo",
        ]

    @pytest.mark.asyncio
    async def test_kill_window(self):
        controller = TmuxController()
        with patch(
            "asyncio.create_subprocess_exec", return_value=_mock_proc()
        ) as mock_exec:
            await controller.kill_window("demo")
        assert _argv(mock_exec) == [
            "tmux", "kill-window", "-t", "trellm-interactive:demo",
        ]


class TestTmuxConfiguration:
    """Session name and binary path are configurable so M4 / a Humphrey
    port can name their own session without editing the module."""

    def test_default_session_constant(self):
        assert DEFAULT_SESSION == "trellm-interactive"
        assert TmuxController().session == "trellm-interactive"

    @pytest.mark.asyncio
    async def test_custom_session_and_binary_used_in_argv(self):
        controller = TmuxController(session="humphrey-interactive", binary="/opt/tmux")
        assert controller.session == "humphrey-interactive"
        with patch(
            "asyncio.create_subprocess_exec", return_value=_mock_proc()
        ) as mock_exec:
            await controller.kill_window("hp")
        assert _argv(mock_exec) == [
            "/opt/tmux", "kill-window", "-t", "humphrey-interactive:hp",
        ]


class TestTmuxErrorHandling:
    """A non-zero tmux exit on a checked command must surface as TmuxError
    carrying the argv, exit code, and stderr."""

    @pytest.mark.asyncio
    async def test_nonzero_exit_raises_tmux_error(self):
        controller = TmuxController()
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=_mock_proc(returncode=1, stderr=b"can't find window: demo"),
        ):
            with pytest.raises(TmuxError) as excinfo:
                await controller.kill_window("demo")
        err = excinfo.value
        assert err.returncode == 1
        assert "can't find window" in err.stderr
        assert err.command == [
            "tmux", "kill-window", "-t", "trellm-interactive:demo",
        ]

    def test_tmux_error_is_runtime_error(self):
        """TmuxError subclasses RuntimeError so callers can catch broadly."""
        assert issubclass(TmuxError, RuntimeError)


@pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux binary not installed"
)
class TestTmuxIntegration:
    """One end-to-end test against a real `tmux`, exercising every method.

    Uses a PID-scoped session name so a real `trellm-interactive` session
    on the host is never touched, and tears the session down afterwards.
    """

    @pytest.mark.asyncio
    async def test_full_lifecycle_against_real_tmux(self, tmp_path):
        session = f"trellm-itest-{os.getpid()}"
        controller = TmuxController(session=session)
        try:
            # Cold start: no session, no windows.
            assert await controller.has_session() is False
            assert await controller.list_windows() == []

            # create_session is idempotent.
            assert await controller.create_session() is True
            assert await controller.has_session() is True
            assert await controller.create_session() is False

            # A window started in the project's working dir.
            await controller.create_window("proj1", str(tmp_path))
            assert "proj1" in await controller.list_windows()

            # send_literal types text, send_keys submits it; capture_pane
            # then shows the resulting output.
            marker = "trellm-tmux-integration-ok"
            await controller.send_literal("proj1", f"echo {marker}")
            await controller.send_keys("proj1", "Enter")
            await asyncio.sleep(1.0)  # let the shell run the command
            assert marker in await controller.capture_pane("proj1")

            # kill_window removes just that window.
            await controller.kill_window("proj1")
            assert "proj1" not in await controller.list_windows()
        finally:
            subprocess.run(
                ["tmux", "kill-session", "-t", session],
                capture_output=True,
            )
