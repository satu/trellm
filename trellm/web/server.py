"""Embedded aiohttp web server for TreLLM dashboard."""

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from aiohttp import web

from ..claude import fetch_claude_usage_limits
from ..config import Config
from ..state import StateManager
from ..trello import TrelloCard

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def _seconds_since(iso_timestamp: Optional[str]) -> int:
    """Return seconds elapsed since an ISO-8601 timestamp, or -1 on failure.

    Accepts both 'Z' and explicit UTC offsets — Trello uses the 'Z' suffix.
    """
    if not iso_timestamp:
        return -1
    try:
        parsed = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - parsed
        return int(delta.total_seconds())
    except (ValueError, TypeError):
        return -1


class WebServer:
    """Embedded web server providing dashboard API and static files."""

    def __init__(
        self,
        config: Config,
        state: StateManager,
        running_tasks: set[asyncio.Task],
        processing_cards: set[str],
        start_time: float,
        card_retry_state: Optional[dict] = None,
    ):
        self.config = config
        self.state = state
        self.running_tasks = running_tasks
        self.processing_cards = processing_cards
        self.start_time = start_time
        # Per-card retry state from __main__.py; allows queue/task endpoints
        # to surface error/timeout counts + backoff status. Defaults to an
        # empty local dict so tests/standalone use don't crash on access.
        self.card_retry_state: dict = card_retry_state if card_retry_state is not None else {}
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._task_info: dict[str, dict] = {}  # task_id -> {project, card_name, card_url, started_at}
        self._on_abort: Optional[Callable[[], asyncio.Future]] = None
        self._on_restart: Optional[Callable[[], asyncio.Future]] = None
        self._usage_cooldown = 300  # Minimum seconds between API calls (5 min)
        # Load persisted usage cache from state (survives restarts)
        persisted = self.state.state.get("usage_cache", {})
        self._usage_cache: Optional[dict] = persisted.get("data")
        self._usage_cache_time: float = persisted.get("timestamp", 0)
        self._task_output: dict[str, deque[str]] = {}  # card_id -> output lines
        self._task_output_subscribers: dict[str, list[asyncio.Queue]] = {}
        self._output_buffer_limit = 5000  # Max lines per task
        self._completed_tasks: list[dict] = []  # Last N completed tasks with output
        self._max_completed_tasks = 10
        # Latest TODO snapshot pushed by the polling loop; powers /api/queue.
        # Each entry: {card_id, card_name, card_url, last_activity}.
        self._queue_snapshot: list[dict] = []

    def track_task(self, card_id: str, project: str, card_name: str, card_url: str) -> None:
        """Register a task for dashboard visibility."""
        self._task_info[card_id] = {
            "project": project,
            "card_name": card_name,
            "card_url": card_url,
            "started_at": time.time(),
        }
        self._task_output[card_id] = deque(maxlen=self._output_buffer_limit)
        self._task_output_subscribers[card_id] = []

    def untrack_task(self, card_id: str, success: bool = True) -> None:
        """Remove a completed/cancelled task from tracking.

        When `success` is False, the task is dropped from "recent completions"
        — failed/cancelled runs (e.g. org limit hits) shouldn't pollute the
        list users browse for finished work. Live subscribers are still
        notified so /api/stream connections close cleanly.
        """
        info = self._task_info.pop(card_id, None)
        output = self._task_output.pop(card_id, None)
        # Preserve only successful runs in completed tasks list
        if success and info and output:
            completed_at = time.time()
            run_id = f"{card_id}_{int(completed_at)}"
            self._completed_tasks.insert(0, {
                "card_id": card_id,
                "run_id": run_id,
                "project": info["project"],
                "card_name": info["card_name"],
                "card_url": info["card_url"],
                "started_at": info["started_at"],
                "completed_at": completed_at,
                "output": list(output),
            })
            # Keep only last N
            if len(self._completed_tasks) > self._max_completed_tasks:
                self._completed_tasks = self._completed_tasks[:self._max_completed_tasks]
        # Signal subscribers that the task is done
        for queue in self._task_output_subscribers.pop(card_id, []):
            queue.put_nowait(None)

    def append_output(self, card_id: str, line: str) -> None:
        """Append an output line for a running task."""
        buf = self._task_output.get(card_id)
        if buf is None:
            logger.debug("append_output: no buffer for card %s (not tracked)", card_id)
            return
        buf.append(line)
        subs = self._task_output_subscribers.get(card_id, [])
        if len(buf) <= 3 or len(buf) % 50 == 0:
            logger.debug(
                "append_output: card=%s lines=%d subscribers=%d preview=%s",
                card_id[:8], len(buf), len(subs), repr(line[:80]),
            )
        for queue in subs:
            queue.put_nowait(line)

    def get_output(self, task_id: str) -> list[str]:
        """Get all buffered output lines for a task (running or completed).

        Args:
            task_id: Either a card_id (for running tasks) or a run_id (for completed tasks)
        """
        # Check running tasks first (card_id match)
        buf = self._task_output.get(task_id)
        if buf is not None:
            return list(buf)
        # Check completed tasks by run_id, then by card_id
        for task in self._completed_tasks:
            if task.get("run_id") == task_id or task["card_id"] == task_id:
                return list(task.get("output", []))
        return []

    def get_completed_tasks(self) -> list[dict]:
        """Get the list of recently completed tasks."""
        return [
            {
                "card_id": t["card_id"],
                "run_id": t.get("run_id", t["card_id"]),
                "project": t["project"],
                "card_name": t["card_name"],
                "card_url": t["card_url"],
                "started_at": t["started_at"],
                "completed_at": t["completed_at"],
                "output_lines": len(t.get("output", [])),
            }
            for t in self._completed_tasks
        ]

    def set_callbacks(
        self,
        on_abort: Callable[[], asyncio.Future],
        on_restart: Callable[[], asyncio.Future],
    ) -> None:
        """Set callbacks for control actions."""
        self._on_abort = on_abort
        self._on_restart = on_restart

    async def refresh_usage_limits(self) -> None:
        """Fetch and cache Claude usage limits.

        Strictly rate-limited to one API call per cooldown period (5 min).
        All callers go through this same gate — no exceptions.
        """
        if self._usage_cache_time > 0:
            elapsed = time.time() - self._usage_cache_time
            if elapsed < self._usage_cooldown:
                logger.info(
                    "Usage API: skipped (%.0fs since last call, cooldown=%ds)",
                    elapsed, self._usage_cooldown,
                )
                return
        logger.info(
            "Usage API: calling fetch_claude_usage_limits (last_call=%.0fs ago)",
            time.time() - self._usage_cache_time if self._usage_cache_time else -1,
        )
        try:
            loop = asyncio.get_event_loop()
            usage_limits = await loop.run_in_executor(None, fetch_claude_usage_limits)
            formatted = self._format_usage_data(usage_limits)
            self._usage_cache = formatted
            if usage_limits.error:
                # API returned an error (e.g. 429) — cache the error but
                # DON'T update cooldown timer so we can retry on next call
                logger.warning("Usage API: error in response: %s", usage_limits.error)
            else:
                # Success — start cooldown timer and persist to state
                self._usage_cache_time = time.time()
                self.state.state["usage_cache"] = {
                    "data": self._usage_cache,
                    "timestamp": self._usage_cache_time,
                }
                self.state._save()
                logger.info("Usage API: success (persisted to state)")
        except Exception as e:
            logger.warning("Usage API: exception: %s", e)
            self._usage_cache = {"error": str(e)}

    @staticmethod
    def _format_usage_data(usage_limits) -> dict:
        """Format usage limits into JSON-serializable dict."""
        data: dict = {}
        if usage_limits.error:
            data["error"] = usage_limits.error
            return data
        if usage_limits.five_hour:
            data["five_hour"] = {
                "utilization": usage_limits.five_hour.utilization,
                "resets_at": usage_limits.five_hour.format_reset_time(),
            }
        if usage_limits.seven_day:
            data["seven_day"] = {
                "utilization": usage_limits.seven_day.utilization,
                "resets_at": usage_limits.seven_day.format_reset_time(),
            }
        if usage_limits.seven_day_opus and usage_limits.seven_day_opus.utilization > 0:
            data["seven_day_opus"] = {
                "utilization": usage_limits.seven_day_opus.utilization,
                "resets_at": usage_limits.seven_day_opus.format_reset_time(),
            }
        if usage_limits.seven_day_sonnet and usage_limits.seven_day_sonnet.utilization > 0:
            data["seven_day_sonnet"] = {
                "utilization": usage_limits.seven_day_sonnet.utilization,
                "resets_at": usage_limits.seven_day_sonnet.format_reset_time(),
            }
        return data

    def update_config(self, config: Config) -> None:
        """Update config reference after hot reload."""
        self.config = config

    def update_queue(self, cards: list[TrelloCard]) -> None:
        """Replace the TODO snapshot. Called each poll cycle from the
        polling loop. Stored as a list of plain dicts so the endpoint
        handler doesn't need to know about TrelloCard."""
        self._queue_snapshot = [
            {
                "card_id": c.id,
                "card_name": c.name,
                "card_url": c.url,
                "last_activity": c.last_activity,
            }
            for c in cards
        ]

    def _create_app(self) -> web.Application:
        app = web.Application(middlewares=[self._no_cache_static_middleware])
        app.router.add_get("/api/status", self._handle_status)
        app.router.add_get("/api/tasks", self._handle_tasks)
        app.router.add_get("/api/queue", self._handle_queue)
        app.router.add_get("/api/projects", self._handle_projects)
        app.router.add_get("/api/stats", self._handle_stats)
        app.router.add_post("/api/abort", self._handle_abort)
        app.router.add_post("/api/restart", self._handle_restart)
        app.router.add_post("/api/usage/refresh", self._handle_usage_refresh)
        app.router.add_get("/api/stream/{card_id}", self._handle_stream)
        app.router.add_get("/api/config", self._handle_config)
        app.router.add_get("/api/completed", self._handle_completed)
        # Serve static files (index.html at root)
        app.router.add_get("/", self._handle_index)
        app.router.add_static("/static", STATIC_DIR, show_index=False)
        return app

    @web.middleware
    async def _no_cache_static_middleware(self, request, handler):
        """Prevent browser caching of static files."""
        response = await handler(request)
        if request.path.startswith("/static") or request.path == "/":
            response.headers["Cache-Control"] = "no-cache"
        return response

    async def start(self) -> None:
        """Start the web server."""
        self._app = self._create_app()
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(
            self._runner,
            self.config.web.host,
            self.config.web.port,
        )
        await site.start()
        logger.info(
            "Web dashboard started at http://%s:%d",
            self.config.web.host,
            self.config.web.port,
        )

    async def stop(self) -> None:
        """Stop the web server."""
        if self._runner:
            await self._runner.cleanup()
            logger.info("Web dashboard stopped")

    async def _handle_index(self, request: web.Request) -> web.Response:
        index_path = STATIC_DIR / "index.html"
        if not index_path.exists():
            return web.Response(text="Dashboard not found", status=404)
        return web.FileResponse(index_path)

    async def _handle_status(self, request: web.Request) -> web.Response:
        uptime = time.time() - self.start_time
        projects = {}
        for name, proj in self.config.claude.projects.items():
            projects[name] = {
                "working_dir": proj.working_dir,
                "aliases": proj.aliases,
            }

        data = {
            "status": "running",
            "uptime_seconds": int(uptime),
            "poll_interval": self.config.poll_interval,
            "active_tasks": len(self._task_info),
            "projects": projects,
        }
        return web.json_response(data)

    async def _handle_tasks(self, request: web.Request) -> web.Response:
        now = time.time()
        tasks = []
        for card_id, info in self._task_info.items():
            retry = self.card_retry_state.get(card_id)
            tasks.append({
                "card_id": card_id,
                "project": info["project"],
                "card_name": info["card_name"],
                "card_url": info["card_url"],
                "duration_seconds": int(now - info["started_at"]),
                "has_output": card_id in self._task_output and len(self._task_output[card_id]) > 0,
                "output_lines": len(self._task_output.get(card_id, [])),
                "error_count": retry.error_count if retry else 0,
                "timeout_count": retry.timeout_count if retry else 0,
                "fast_failure_streak": retry.fast_failure_streak if retry else 0,
            })
        return web.json_response({"tasks": tasks})

    async def _handle_queue(self, request: web.Request) -> web.Response:
        """Return the latest TODO snapshot with project / retry / running
        state merged in. Powers the dashboard's queue view.

        Each entry has: card_id, card_name, card_url, project,
        queued_for_seconds, is_running, and (optionally) retry counters
        + backoff_remaining_seconds.
        """
        now = time.time()
        items = []
        for entry in self._queue_snapshot:
            card_id = entry["card_id"]
            card_name = entry["card_name"] or ""
            # Project resolution mirrors __main__.parse_project so aliased
            # cards group with their canonical project name.
            parts = card_name.split()
            parsed = parts[0].rstrip(":").lower() if parts else "unknown"
            project = self.config.resolve_project(parsed) or parsed

            queued_for = _seconds_since(entry.get("last_activity"))

            retry = self.card_retry_state.get(card_id)
            retry_info = None
            if retry is not None:
                retry_info = {
                    "error_count": retry.error_count,
                    "timeout_count": retry.timeout_count,
                    "fast_failure_streak": retry.fast_failure_streak,
                    "backoff_remaining_seconds": retry.seconds_until_resume(now=now),
                }

            items.append({
                "card_id": card_id,
                "card_name": card_name,
                "card_url": entry["card_url"],
                "project": project,
                "queued_for_seconds": queued_for,
                "is_running": card_id in self.processing_cards,
                "retry": retry_info,
            })
        return web.json_response({"queue": items})

    async def _handle_projects(self, request: web.Request) -> web.Response:
        projects = []
        for name, proj in self.config.claude.projects.items():
            # Get last execution info from state
            session_data = self.state.state.get("sessions", {}).get(name, {})
            last_card_id = session_data.get("last_card_id")
            last_activity = session_data.get("last_activity")

            # Get per-project stats
            proj_stats = self.state.get_stats(name)

            projects.append({
                "name": name,
                "working_dir": proj.working_dir,
                "aliases": proj.aliases,
                "last_card_id": last_card_id,
                "last_activity": last_activity,
                "stats": {
                    "total_cost_dollars": proj_stats.total_cost_dollars,
                    "total_tickets": proj_stats.total_tickets,
                    "average_cost_dollars": proj_stats.average_cost_dollars,
                    "total_lines_added": proj_stats.total_lines_added,
                    "total_lines_removed": proj_stats.total_lines_removed,
                },
            })
        return web.json_response({"projects": projects})

    async def _handle_stats(self, request: web.Request) -> web.Response:
        global_stats = self.state.get_stats()
        last_30 = self.state.get_stats_for_period(30)

        # Use cached usage limits (refreshed after ticket completion or manually)
        usage_data = self._usage_cache or {}
        cache_age = int(time.time() - self._usage_cache_time) if self._usage_cache_time else -1

        # Per-project stats
        by_project = {}
        for name in self.config.claude.projects:
            ps = self.state.get_stats(name)
            by_project[name] = {
                "total_cost_dollars": ps.total_cost_dollars,
                "total_tickets": ps.total_tickets,
                "average_cost_dollars": ps.average_cost_dollars,
                "total_lines_added": ps.total_lines_added,
                "total_lines_removed": ps.total_lines_removed,
                "total_tokens": ps.total_tokens_formatted,
                "input_tokens": ps.input_tokens_formatted,
                "output_tokens": ps.output_tokens_formatted,
            }

        # Recent ticket history
        history = self.state.state.get("stats", {}).get("ticket_history", [])
        recent_history = history[-20:] if history else []

        data = {
            "usage_limits": usage_data,
            "usage_cache_age_seconds": cache_age,
            "global": {
                "total_cost_dollars": global_stats.total_cost_dollars,
                "total_tickets": global_stats.total_tickets,
                "average_cost_dollars": global_stats.average_cost_dollars,
                "api_duration": global_stats.api_duration_formatted,
                "wall_duration": global_stats.wall_duration_formatted,
                "total_lines_added": global_stats.total_lines_added,
                "total_lines_removed": global_stats.total_lines_removed,
                "total_tokens": global_stats.total_tokens_formatted,
                "input_tokens": global_stats.input_tokens_formatted,
                "output_tokens": global_stats.output_tokens_formatted,
                "cache_read_tokens": global_stats.cache_read_tokens_formatted,
            },
            "last_30_days": {
                "total_cost_dollars": last_30.total_cost_dollars,
                "total_tickets": last_30.total_tickets,
                "average_cost_dollars": last_30.average_cost_dollars,
                "total_tokens": last_30.total_tokens_formatted,
                "input_tokens": last_30.input_tokens_formatted,
                "output_tokens": last_30.output_tokens_formatted,
            },
            "by_project": by_project,
            "recent_history": recent_history,
        }
        return web.json_response(data)

    async def _handle_abort(self, request: web.Request) -> web.Response:
        if not self._on_abort:
            return web.json_response(
                {"error": "Abort not available"}, status=503,
            )
        try:
            tasks_cancelled = len(self.running_tasks)
            await self._on_abort()
            self._task_info.clear()
            return web.json_response({
                "success": True,
                "tasks_cancelled": tasks_cancelled,
            })
        except Exception as e:
            logger.error("Abort failed: %s", e)
            return web.json_response(
                {"error": str(e)}, status=500,
            )

    async def _handle_usage_refresh(self, request: web.Request) -> web.Response:
        await self.refresh_usage_limits()
        cache_age = int(time.time() - self._usage_cache_time) if self._usage_cache_time else -1
        return web.json_response({
            "success": True,
            "usage_limits": self._usage_cache or {},
            "cache_age_seconds": cache_age,
        })

    async def _handle_restart(self, request: web.Request) -> web.Response:
        if not self._on_restart:
            return web.json_response(
                {"error": "Restart not available"}, status=503,
            )
        try:
            tasks_cancelled = len(self.running_tasks)
            await self._on_restart()
            return web.json_response({
                "success": True,
                "tasks_cancelled": tasks_cancelled,
                "message": "Restart initiated",
            })
        except Exception as e:
            logger.error("Restart failed: %s", e)
            return web.json_response(
                {"error": str(e)}, status=500,
            )

    async def _handle_stream(self, request: web.Request) -> web.StreamResponse:
        task_id = request.match_info["card_id"]  # Can be card_id or run_id
        is_running = task_id in self._task_info
        is_completed = any(
            t.get("run_id") == task_id or t["card_id"] == task_id
            for t in self._completed_tasks
        )
        if not is_running and not is_completed:
            logger.debug("SSE stream: task %s not found", task_id[:8])
            return web.json_response({"error": "Task not found"}, status=404)
        card_id = task_id  # For running task subscriber lookup

        logger.debug(
            "SSE stream: connecting for task %s (running=%s, completed=%s)",
            task_id[:8], is_running, is_completed,
        )

        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        await response.prepare(request)

        # Send existing buffered output
        existing = self.get_output(task_id)
        logger.debug("SSE stream: sending %d existing lines", len(existing))
        for line in existing:
            await response.write(f"data: {line}\n".encode())

        # For completed tasks, send done event immediately
        if not is_running:
            await response.write(b"event: done\ndata: task completed\n\n")
            return response

        # Subscribe to new output (running tasks only)
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        subscribers = self._task_output_subscribers.get(card_id, [])
        subscribers.append(queue)

        try:
            while True:
                line = await queue.get()
                if line is None:
                    # Task completed
                    await response.write(b"event: done\ndata: task completed\n\n")
                    break
                await response.write(f"data: {line}\n".encode())
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            subscribers = self._task_output_subscribers.get(card_id, [])
            if queue in subscribers:
                subscribers.remove(queue)

        return response

    async def _handle_completed(self, request: web.Request) -> web.Response:
        now = time.time()
        # Index the latest ticket_history entry per card so the completed
        # list can surface token counts without each task needing its own
        # cost plumbing. The history list is ordered oldest -> newest, so
        # the last entry for a given card_id is the freshest run.
        history = self.state.state.get("stats", {}).get("ticket_history", [])
        latest_by_card: dict[str, dict] = {}
        for entry in history:
            cid = entry.get("card_id")
            if cid:
                latest_by_card[cid] = entry
        completed = []
        for t in self._completed_tasks:
            hist = latest_by_card.get(t["card_id"], {})
            completed.append({
                "card_id": t["card_id"],
                "run_id": t.get("run_id", t["card_id"]),
                "project": t["project"],
                "card_name": t["card_name"],
                "card_url": t["card_url"],
                "duration_seconds": int(t["completed_at"] - t["started_at"]),
                "completed_ago_seconds": int(now - t["completed_at"]),
                "output_lines": len(t.get("output", [])),
                "input_tokens": hist.get("input_tokens", 0),
                "output_tokens": hist.get("output_tokens", 0),
            })
        return web.json_response({"completed": completed})

    @staticmethod
    def _mask_secret(value: str) -> str:
        """Mask a secret string, showing only first 4 chars."""
        if not value or len(value) <= 4:
            return "***"
        return value[:4] + "***"

    async def _handle_config(self, request: web.Request) -> web.Response:
        cfg = self.config
        projects = {}
        for name, proj in cfg.claude.projects.items():
            proj_data: dict = {
                "working_dir": proj.working_dir,
                "aliases": proj.aliases,
            }
            if proj.compact_prompt:
                proj_data["compact_prompt"] = proj.compact_prompt
            if proj.maintenance:
                proj_data["maintenance"] = {
                    "enabled": proj.maintenance.enabled,
                    "interval": proj.maintenance.interval,
                }
            projects[name] = proj_data

        claude_data: dict = {
            "binary": cfg.claude.binary,
            "timeout": cfg.claude.timeout,
            "yolo": cfg.claude.yolo,
            "projects": projects,
        }
        if cfg.claude.maintenance:
            claude_data["maintenance"] = {
                "enabled": cfg.claude.maintenance.enabled,
                "interval": cfg.claude.maintenance.interval,
            }

        data = {
            "trello": {
                "api_key": self._mask_secret(cfg.trello.api_key),
                "api_token": self._mask_secret(cfg.trello.api_token),
                "board_id": cfg.trello.board_id,
                "todo_list_id": cfg.trello.todo_list_id,
            },
            "claude": claude_data,
            "web": {
                "enabled": cfg.web.enabled,
                "host": cfg.web.host,
                "port": cfg.web.port,
            },
            "poll_interval": cfg.poll_interval,
        }
        return web.json_response(data)
