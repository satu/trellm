"""Low-level async wrapper around the `tmux` binary.

`docs/claude-interactive.md` plans a second `claude` transport — a long-lived
interactive TUI per project, each running in its own tmux window — so cards
stay on the subscription seat instead of becoming metered `claude -p` calls.
M2 (this module) is the low-level tmux wrapper that path needs, and *only*
that: it creates and kills windows, lists them, types keystrokes, and
captures pane text. It knows nothing about `claude`; M4 layers
`InteractiveSession` on top.

The window layout the higher layers assume (doc §6.1 / §8): one tmux session
named `trellm-interactive`; one window per project; window name == project
name; each window started in the project's working directory. That is why
the methods here address a window by *project* name.

Every tmux call goes through `asyncio.create_subprocess_exec`, consistent
with `claude.py` and `maintenance.py`.
"""

import asyncio
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# The dedicated tmux session every interactive window lives in (doc §6.1/§8).
DEFAULT_SESSION = "trellm-interactive"


class TmuxError(RuntimeError):
    """A `tmux` command exited non-zero.

    Subclasses `RuntimeError` so callers can catch it broadly. Carries the
    argv, exit code, and captured stderr so the failing tmux invocation is
    visible without re-running it.
    """

    def __init__(
        self,
        message: str,
        *,
        command: list,
        returncode: int,
        stderr: str,
    ):
        super().__init__(message)
        self.command = command
        self.returncode = returncode
        self.stderr = stderr


class TmuxController:
    """Async wrapper over the `tmux` binary for one tmux session.

    All windows live in a single session (default `trellm-interactive`);
    each method targets a window by project name, since the layered design
    keeps window name == project name. Stateless beyond the session and
    binary names — every call shells out to `tmux` fresh.
    """

    def __init__(self, session: str = DEFAULT_SESSION, binary: str = "tmux"):
        self._session = session
        self._binary = binary

    @property
    def session(self) -> str:
        """Name of the tmux session this controller drives."""
        return self._session

    def _target(self, project: str) -> str:
        """tmux target for a project's window: `<session>:<window>`."""
        return f"{self._session}:{project}"

    async def _run(self, *args: str, check: bool = True) -> tuple:
        """Run `tmux <args>`, returning `(returncode, stdout, stderr)`.

        Raises `TmuxError` on a non-zero exit when `check` is True. Pass
        `check=False` for commands whose exit code is itself the answer
        (e.g. `has-session`).
        """
        cmd = [self._binary, *args]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await proc.communicate()
        stdout = stdout_b.decode() if stdout_b else ""
        stderr = stderr_b.decode() if stderr_b else ""
        returncode = proc.returncode or 0
        if check and returncode != 0:
            raise TmuxError(
                f"`tmux {' '.join(args)}` failed (exit {returncode}): "
                f"{stderr.strip()}",
                command=cmd,
                returncode=returncode,
                stderr=stderr,
            )
        return returncode, stdout, stderr

    async def has_session(self) -> bool:
        """True if the controller's tmux session exists."""
        returncode, _, _ = await self._run(
            "has-session", "-t", self._session, check=False
        )
        return returncode == 0

    async def create_session(self) -> bool:
        """Create the detached tmux session if it does not already exist.

        Idempotent: returns True if a new session was created, False if one
        was already running. Mirrors doc §6.1 — `new-session -d` only when
        the session is absent.
        """
        if await self.has_session():
            return False
        await self._run("new-session", "-d", "-s", self._session)
        logger.info("Created tmux session %r", self._session)
        return True

    async def create_window(
        self,
        project: str,
        working_dir: str,
        command: Optional[str] = None,
    ) -> None:
        """Create a detached window for `project` in `working_dir`.

        The window is named after the project and started in its working
        directory (doc §6.1/§8); `~` is expanded as elsewhere in trellm.
        `command`, when given, is the shell command the window runs — M4
        passes the `claude` invocation here; left None the window runs an
        interactive shell.
        """
        cwd = str(Path(working_dir).expanduser())
        args = [
            "new-window", "-d",
            "-t", self._session,
            "-n", project,
            "-c", cwd,
        ]
        if command is not None:
            args.append(command)
        await self._run(*args)
        logger.info(
            "Created tmux window %r in session %r (cwd=%s)",
            project, self._session, cwd,
        )

    async def list_windows(self) -> list:
        """Return the window names in the session (== project names).

        Empty list when the session does not exist — the expected state on
        a cold start, and what doc §6.1's restart path checks.
        """
        if not await self.has_session():
            return []
        _, stdout, _ = await self._run(
            "list-windows", "-t", self._session, "-F", "#{window_name}"
        )
        return [line for line in stdout.splitlines() if line]

    async def send_keys(self, project: str, *keys: str) -> None:
        """Send tmux key names to the project's window.

        Each argument is a tmux key name — `Enter`, `C-c`, `Escape`. Use
        this for control keys and for submitting input; use `send_literal`
        for prompt text.
        """
        if not keys:
            raise ValueError("send_keys requires at least one key")
        await self._run("send-keys", "-t", self._target(project), *keys)

    async def send_literal(self, project: str, text: str) -> None:
        """Type `text` verbatim into the project's window (`send-keys -l`).

        The `-l` flag makes tmux treat the argument literally rather than as
        key names, so spaces, punctuation, and words like `Enter` are sent
        as characters. Per doc §8 a prompt is typed with `send_literal`,
        then submitted with a separate `send_keys(project, "Enter")`.
        """
        await self._run("send-keys", "-t", self._target(project), "-l", text)

    async def capture_pane(self, project: str) -> str:
        """Return the visible pane text of the project's window.

        Feeds the dashboard's best-effort live view (doc §6.3); never a
        load-bearing completion signal.
        """
        _, stdout, _ = await self._run(
            "capture-pane", "-p", "-t", self._target(project)
        )
        return stdout

    async def kill_window(self, project: str) -> None:
        """Kill the project's window."""
        await self._run("kill-window", "-t", self._target(project))
        logger.info("Killed tmux window %r", project)
