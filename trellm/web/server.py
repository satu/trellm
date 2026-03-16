"""Embedded aiohttp web server for TreLLM dashboard."""

import asyncio
import logging
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional

from aiohttp import web

from ..claude import fetch_claude_usage_limits
from ..config import Config
from ..state import StateManager

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class WebServer:
    """Embedded web server providing dashboard API and static files."""

    def __init__(
        self,
        config: Config,
        state: StateManager,
        running_tasks: set[asyncio.Task],
        processing_cards: set[str],
        start_time: float,
    ):
        self.config = config
        self.state = state
        self.running_tasks = running_tasks
        self.processing_cards = processing_cards
        self.start_time = start_time
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._task_info: dict[str, dict] = {}  # task_id -> {project, card_name, card_url, started_at}
        self._on_abort: Optional[Callable[[], asyncio.Future]] = None
        self._on_restart: Optional[Callable[[], asyncio.Future]] = None
        self._usage_cache: Optional[dict] = None  # Cached usage limits data
        self._usage_cache_time: float = 0  # When cache was last updated
        self._task_output: dict[str, deque[str]] = {}  # card_id -> output lines
        self._task_output_subscribers: dict[str, list[asyncio.Queue]] = {}
        self._output_buffer_limit = 5000  # Max lines per task

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

    def untrack_task(self, card_id: str) -> None:
        """Remove a completed/cancelled task from tracking."""
        self._task_info.pop(card_id, None)
        self._task_output.pop(card_id, None)
        # Signal subscribers that the task is done
        for queue in self._task_output_subscribers.pop(card_id, []):
            queue.put_nowait(None)

    def append_output(self, card_id: str, line: str) -> None:
        """Append an output line for a running task."""
        buf = self._task_output.get(card_id)
        if buf is None:
            return
        buf.append(line)
        for queue in self._task_output_subscribers.get(card_id, []):
            queue.put_nowait(line)

    def get_output(self, card_id: str) -> list[str]:
        """Get all buffered output lines for a task."""
        buf = self._task_output.get(card_id)
        if buf is None:
            return []
        return list(buf)

    def set_callbacks(
        self,
        on_abort: Callable[[], asyncio.Future],
        on_restart: Callable[[], asyncio.Future],
    ) -> None:
        """Set callbacks for control actions."""
        self._on_abort = on_abort
        self._on_restart = on_restart

    async def refresh_usage_limits(self) -> None:
        """Fetch and cache Claude usage limits. Called after ticket completion."""
        try:
            loop = asyncio.get_event_loop()
            usage_limits = await loop.run_in_executor(None, fetch_claude_usage_limits)
            self._usage_cache = self._format_usage_data(usage_limits)
            self._usage_cache_time = time.time()
        except Exception as e:
            logger.warning("Failed to refresh usage limits: %s", e)
            self._usage_cache = {"error": str(e)}
            self._usage_cache_time = time.time()

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

    def _create_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/api/status", self._handle_status)
        app.router.add_get("/api/tasks", self._handle_tasks)
        app.router.add_get("/api/projects", self._handle_projects)
        app.router.add_get("/api/stats", self._handle_stats)
        app.router.add_post("/api/abort", self._handle_abort)
        app.router.add_post("/api/restart", self._handle_restart)
        app.router.add_post("/api/usage/refresh", self._handle_usage_refresh)
        app.router.add_get("/api/stream/{card_id}", self._handle_stream)
        # Serve static files (index.html at root)
        app.router.add_get("/", self._handle_index)
        app.router.add_static("/static", STATIC_DIR, show_index=False)
        return app

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
            tasks.append({
                "card_id": card_id,
                "project": info["project"],
                "card_name": info["card_name"],
                "card_url": info["card_url"],
                "duration_seconds": int(now - info["started_at"]),
                "has_output": card_id in self._task_output and len(self._task_output[card_id]) > 0,
            })
        return web.json_response({"tasks": tasks})

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
        return web.json_response({
            "success": True,
            "usage_limits": self._usage_cache or {},
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
        card_id = request.match_info["card_id"]
        if card_id not in self._task_info:
            return web.json_response({"error": "Task not found"}, status=404)

        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        await response.prepare(request)

        # Send existing buffered output
        for line in self.get_output(card_id):
            await response.write(f"data: {line}".encode())

        # Subscribe to new output
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
                await response.write(f"data: {line}".encode())
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            subscribers = self._task_output_subscribers.get(card_id, [])
            if queue in subscribers:
                subscribers.remove(queue)

        return response
