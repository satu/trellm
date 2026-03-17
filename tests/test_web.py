"""Tests for web dashboard server."""

import asyncio
import time
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer

from trellm.claude import ClaudeUsageLimits, UsageLimitInfo
from trellm.config import Config, TrelloConfig, ClaudeConfig, ProjectConfig, WebConfig
from trellm.state import StateManager
from trellm.web.server import WebServer


def _mock_usage_limits():
    """Return mock usage limits for testing."""
    return ClaudeUsageLimits(
        five_hour=UsageLimitInfo(utilization=42.5),
        seven_day=UsageLimitInfo(utilization=15.0),
    )


def _make_config(**overrides) -> Config:
    """Create a test config."""
    defaults = {
        "trello": TrelloConfig(
            api_key="key", api_token="token", board_id="board", todo_list_id="list",
        ),
        "claude": ClaudeConfig(
            projects={
                "testproject": ProjectConfig(
                    working_dir="~/src/testproject",
                    aliases=["tp"],
                ),
                "other": ProjectConfig(
                    working_dir="~/src/other",
                ),
            }
        ),
        "web": WebConfig(enabled=True, host="127.0.0.1", port=0),
    }
    defaults.update(overrides)
    return Config(**defaults)


def _make_state(tmp_path) -> StateManager:
    """Create a test StateManager."""
    state_file = str(tmp_path / "state.json")
    return StateManager(state_file)


def _make_web_server(config, state) -> WebServer:
    """Create a WebServer for testing."""
    return WebServer(
        config=config,
        state=state,
        running_tasks=set(),
        processing_cards=set(),
        start_time=time.time() - 120,  # 2 minutes ago
    )


@pytest.fixture(autouse=True)
def mock_usage_limits():
    """Mock fetch_claude_usage_limits for all tests."""
    with patch("trellm.web.server.fetch_claude_usage_limits", return_value=_mock_usage_limits()):
        yield


@pytest.fixture
def config():
    return _make_config()


@pytest.fixture
def state(tmp_path):
    return _make_state(tmp_path)


@pytest.fixture
def web_server(config, state):
    return _make_web_server(config, state)


@pytest.fixture
async def client(web_server):
    """Create an aiohttp test client for the web server."""
    app = web_server._create_app()
    async with TestClient(TestServer(app)) as client:
        yield client


class TestWebServerStatus:
    """Tests for /api/status endpoint."""

    async def test_status_returns_running(self, client):
        resp = await client.get("/api/status")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "running"

    async def test_status_has_uptime(self, client):
        resp = await client.get("/api/status")
        data = await resp.json()
        assert data["uptime_seconds"] >= 0

    async def test_status_has_projects(self, client):
        resp = await client.get("/api/status")
        data = await resp.json()
        assert "testproject" in data["projects"]
        assert "other" in data["projects"]
        assert data["projects"]["testproject"]["aliases"] == ["tp"]

    async def test_status_has_poll_interval(self, client):
        resp = await client.get("/api/status")
        data = await resp.json()
        assert data["poll_interval"] == 5

    async def test_status_active_tasks_zero(self, client):
        resp = await client.get("/api/status")
        data = await resp.json()
        assert data["active_tasks"] == 0


class TestWebServerTasks:
    """Tests for /api/tasks endpoint."""

    async def test_tasks_empty(self, client):
        resp = await client.get("/api/tasks")
        assert resp.status == 200
        data = await resp.json()
        assert data["tasks"] == []

    async def test_tasks_with_tracked_task(self, web_server):
        web_server.track_task("card123", "testproject", "Fix bug", "https://trello.com/c/abc")
        app = web_server._create_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/tasks")
            data = await resp.json()
            assert len(data["tasks"]) == 1
            task = data["tasks"][0]
            assert task["card_id"] == "card123"
            assert task["project"] == "testproject"
            assert task["card_name"] == "Fix bug"
            assert task["card_url"] == "https://trello.com/c/abc"
            assert task["duration_seconds"] >= 0

    async def test_untrack_task(self, web_server):
        web_server.track_task("card123", "testproject", "Fix bug", "https://trello.com/c/abc")
        web_server.untrack_task("card123")
        app = web_server._create_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/tasks")
            data = await resp.json()
            assert data["tasks"] == []


class TestWebServerProjects:
    """Tests for /api/projects endpoint."""

    async def test_projects_list(self, client):
        resp = await client.get("/api/projects")
        assert resp.status == 200
        data = await resp.json()
        assert len(data["projects"]) == 2
        names = [p["name"] for p in data["projects"]]
        assert "testproject" in names
        assert "other" in names

    async def test_projects_include_stats(self, client):
        resp = await client.get("/api/projects")
        data = await resp.json()
        for proj in data["projects"]:
            assert "stats" in proj
            assert "total_cost_dollars" in proj["stats"]
            assert "total_tickets" in proj["stats"]


class TestWebServerStats:
    """Tests for /api/stats endpoint."""

    async def test_stats_structure(self, client):
        resp = await client.get("/api/stats")
        assert resp.status == 200
        data = await resp.json()
        assert "global" in data
        assert "last_30_days" in data
        assert "by_project" in data
        assert "recent_history" in data
        assert "usage_limits" in data

    async def test_stats_usage_limits_empty_by_default(self, client):
        resp = await client.get("/api/stats")
        data = await resp.json()
        assert data["usage_limits"] == {}

    async def test_stats_usage_limits_after_refresh(self, web_server):
        await web_server.refresh_usage_limits()
        app = web_server._create_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/stats")
            data = await resp.json()
            ul = data["usage_limits"]
            assert ul["five_hour"]["utilization"] == 42.5
            assert ul["seven_day"]["utilization"] == 15.0

    async def test_stats_usage_limits_error(self, web_server):
        with patch("trellm.web.server.fetch_claude_usage_limits",
                    return_value=ClaudeUsageLimits(error="No token")):
            await web_server.refresh_usage_limits()
        app = web_server._create_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/stats")
            data = await resp.json()
            assert data["usage_limits"]["error"] == "No token"

    async def test_stats_global_fields(self, client):
        resp = await client.get("/api/stats")
        data = await resp.json()
        g = data["global"]
        assert "total_cost_dollars" in g
        assert "total_tickets" in g
        assert "total_tokens" in g

    async def test_stats_with_recorded_data(self, config, tmp_path):
        state = _make_state(tmp_path)
        state.record_cost(
            card_id="card1",
            project="testproject",
            total_cost="$1.50",
            api_duration="2m 30s",
            wall_duration="5m",
            code_changes="+100 -20",
        )
        server = _make_web_server(config, state)
        app = server._create_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/stats")
            data = await resp.json()
            assert data["global"]["total_cost_dollars"] == "$1.50"
            assert data["global"]["total_tickets"] == 1
            assert data["by_project"]["testproject"]["total_tickets"] == 1


class TestWebServerIndex:
    """Tests for serving the dashboard HTML."""

    async def test_index_serves_html(self, client):
        resp = await client.get("/")
        assert resp.status == 200
        text = await resp.text()
        assert "TreLLM Dashboard" in text


class TestWebServerTrackTask:
    """Tests for task tracking."""

    def test_track_and_status(self, web_server):
        web_server.track_task("c1", "proj", "Card 1", "url1")
        web_server.track_task("c2", "proj", "Card 2", "url2")
        assert len(web_server._task_info) == 2

    def test_untrack_nonexistent(self, web_server):
        # Should not raise
        web_server.untrack_task("nonexistent")

    def test_update_config(self, web_server):
        new_config = _make_config(poll_interval=30)
        web_server.update_config(new_config)
        assert web_server.config.poll_interval == 30


class TestWebServerUsageRefresh:
    """Tests for POST /api/usage/refresh endpoint."""

    async def test_usage_refresh(self, client):
        resp = await client.post("/api/usage/refresh")
        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True
        assert "usage_limits" in data
        assert data["usage_limits"]["five_hour"]["utilization"] == 42.5

    async def test_usage_refresh_updates_cache(self, web_server):
        app = web_server._create_app()
        async with TestClient(TestServer(app)) as client:
            # Initially empty
            resp = await client.get("/api/stats")
            data = await resp.json()
            assert data["usage_limits"] == {}

            # After refresh
            await client.post("/api/usage/refresh")
            resp = await client.get("/api/stats")
            data = await resp.json()
            assert data["usage_limits"]["five_hour"]["utilization"] == 42.5


    async def test_usage_refresh_respects_cooldown(self, web_server):
        """Refresh should skip the API call if called again within cooldown."""
        call_count = 0
        original_limits = _mock_usage_limits()

        def counting_fetch():
            nonlocal call_count
            call_count += 1
            return original_limits

        with patch("trellm.web.server.fetch_claude_usage_limits", side_effect=counting_fetch):
            await web_server.refresh_usage_limits()
            assert call_count == 1
            # Second call within cooldown should be skipped
            await web_server.refresh_usage_limits()
            assert call_count == 1

    async def test_usage_refresh_force_bypasses_cooldown(self, web_server):
        """Force refresh should bypass the cooldown."""
        call_count = 0
        original_limits = _mock_usage_limits()

        def counting_fetch():
            nonlocal call_count
            call_count += 1
            return original_limits

        with patch("trellm.web.server.fetch_claude_usage_limits", side_effect=counting_fetch):
            await web_server.refresh_usage_limits()
            assert call_count == 1
            # Force refresh should always call the API
            await web_server.refresh_usage_limits(force=True)
            assert call_count == 2


class TestWebServerAbort:
    """Tests for POST /api/abort endpoint."""

    async def test_abort_without_callback(self, client):
        resp = await client.post("/api/abort")
        assert resp.status == 503
        data = await resp.json()
        assert "error" in data

    async def test_abort_with_callback(self, web_server):
        abort_called = False

        async def mock_abort():
            nonlocal abort_called
            abort_called = True

        web_server.set_callbacks(on_abort=mock_abort, on_restart=mock_abort)
        app = web_server._create_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/abort")
            assert resp.status == 200
            data = await resp.json()
            assert data["success"] is True
            assert abort_called

    async def test_abort_clears_task_info(self, web_server):
        async def mock_abort():
            pass

        web_server.set_callbacks(on_abort=mock_abort, on_restart=mock_abort)
        web_server.track_task("c1", "proj", "Card", "url")
        app = web_server._create_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/abort")
            assert resp.status == 200
            assert len(web_server._task_info) == 0


class TestWebServerRestart:
    """Tests for POST /api/restart endpoint."""

    async def test_restart_without_callback(self, client):
        resp = await client.post("/api/restart")
        assert resp.status == 503
        data = await resp.json()
        assert "error" in data

    async def test_restart_with_callback(self, web_server):
        restart_called = False

        async def mock_abort():
            pass

        async def mock_restart():
            nonlocal restart_called
            restart_called = True

        web_server.set_callbacks(on_abort=mock_abort, on_restart=mock_restart)
        app = web_server._create_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/restart")
            assert resp.status == 200
            data = await resp.json()
            assert data["success"] is True
            assert restart_called

    async def test_restart_reports_error_on_exception(self, web_server):
        async def mock_abort():
            pass

        async def mock_restart():
            raise RuntimeError("test error")

        web_server.set_callbacks(on_abort=mock_abort, on_restart=mock_restart)
        app = web_server._create_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/restart")
            assert resp.status == 500
            data = await resp.json()
            assert "error" in data


class TestWebConfig:
    """Tests for WebConfig in config loading."""

    def test_default_web_config(self):
        config = _make_config()
        # Override with default
        config2 = Config(
            trello=config.trello,
            claude=config.claude,
        )
        assert config2.web.enabled is False
        assert config2.web.host == "0.0.0.0"
        assert config2.web.port == 8077

    def test_web_config_from_yaml(self, tmp_path):
        import yaml
        from trellm.config import load_config

        config_data = {
            "trello": {
                "api_key": "key", "api_token": "token",
                "board_id": "board", "todo_list_id": "list",
            },
            "web": {
                "enabled": True,
                "host": "0.0.0.0",
                "port": 9090,
            },
        }
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_data))

        config = load_config(str(config_file))
        assert config.web.enabled is True
        assert config.web.host == "0.0.0.0"
        assert config.web.port == 9090

    def test_web_config_defaults_from_yaml(self, tmp_path):
        import yaml
        from trellm.config import load_config

        config_data = {
            "trello": {
                "api_key": "key", "api_token": "token",
                "board_id": "board", "todo_list_id": "list",
            },
        }
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_data))

        config = load_config(str(config_file))
        assert config.web.enabled is False
        assert config.web.port == 8077


class TestWebServerOutputBuffer:
    """Tests for per-task output buffering."""

    def test_append_output_stores_lines(self, web_server):
        web_server.track_task("card1", "proj", "test card", "http://example.com")
        web_server.append_output("card1", "line 1\n")
        web_server.append_output("card1", "line 2\n")
        lines = web_server.get_output("card1")
        assert lines == ["line 1\n", "line 2\n"]

    def test_append_output_untracked_card_ignored(self, web_server):
        # Should not raise even if card is not tracked
        web_server.append_output("nonexistent", "some line\n")
        assert web_server.get_output("nonexistent") == []

    def test_get_output_empty(self, web_server):
        assert web_server.get_output("nonexistent") == []

    def test_output_cleared_on_untrack(self, web_server):
        web_server.track_task("card1", "proj", "test card", "http://example.com")
        web_server.append_output("card1", "line 1\n")
        web_server.untrack_task("card1")
        assert web_server.get_output("card1") == []

    def test_output_buffer_limit(self, web_server):
        web_server.track_task("card1", "proj", "test card", "http://example.com")
        # Write more lines than the buffer limit
        for i in range(6000):
            web_server.append_output("card1", f"line {i}\n")
        lines = web_server.get_output("card1")
        assert len(lines) <= 5000
        # Should keep the most recent lines
        assert lines[-1] == "line 5999\n"


class TestWebServerSSEStream:
    """Tests for SSE streaming endpoint."""

    @pytest.mark.asyncio
    async def test_stream_endpoint_exists(self, web_server):
        app = web_server._create_app()
        async with TestClient(TestServer(app)) as client:
            web_server.track_task("card1", "proj", "test", "http://example.com")
            web_server.append_output("card1", "hello\n")
            resp = await client.get("/api/stream/card1")
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "text/event-stream"

    @pytest.mark.asyncio
    async def test_stream_sends_existing_buffer(self, web_server):
        app = web_server._create_app()
        async with TestClient(TestServer(app)) as client:
            web_server.track_task("card1", "proj", "test", "http://example.com")
            web_server.append_output("card1", "line 1\n")
            web_server.append_output("card1", "line 2\n")
            resp = await client.get("/api/stream/card1")
            # Read some data (the existing buffer should be sent)
            data = await resp.content.read(4096)
            text = data.decode()
            assert "line 1" in text
            assert "line 2" in text

    @pytest.mark.asyncio
    async def test_stream_uses_proper_sse_format(self, web_server):
        """SSE events must end with double newline for EventSource to parse."""
        app = web_server._create_app()
        async with TestClient(TestServer(app)) as client:
            web_server.track_task("card1", "proj", "test", "http://example.com")
            web_server.append_output("card1", "hello world\n")
            resp = await client.get("/api/stream/card1")
            data = await resp.content.read(4096)
            text = data.decode()
            # Each SSE event must have "data: ...\n\n" format
            assert "data: hello world\n\n" in text

    @pytest.mark.asyncio
    async def test_stream_unknown_card_returns_404(self, web_server):
        app = web_server._create_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/stream/nonexistent")
            assert resp.status == 404


class TestWebServerConfigViewer:
    """Tests for config viewer endpoint."""

    @pytest.mark.asyncio
    async def test_config_endpoint_exists(self, client):
        resp = await client.get("/api/config")
        assert resp.status == 200
        data = await resp.json()
        assert "trello" in data
        assert "claude" in data
        assert "web" in data

    @pytest.mark.asyncio
    async def test_config_masks_secrets(self, client):
        resp = await client.get("/api/config")
        data = await resp.json()
        # API key and token should be masked
        assert data["trello"]["api_key"] != "key"
        assert data["trello"]["api_token"] != "token"
        assert "***" in data["trello"]["api_key"]
        assert "***" in data["trello"]["api_token"]

    @pytest.mark.asyncio
    async def test_config_shows_projects(self, client):
        resp = await client.get("/api/config")
        data = await resp.json()
        assert "testproject" in data["claude"]["projects"]
        assert data["claude"]["projects"]["testproject"]["working_dir"] == "~/src/testproject"

    @pytest.mark.asyncio
    async def test_config_shows_web_settings(self, client):
        resp = await client.get("/api/config")
        data = await resp.json()
        assert data["web"]["enabled"] is True
        assert "port" in data["web"]

    @pytest.mark.asyncio
    async def test_config_shows_poll_interval(self, client):
        resp = await client.get("/api/config")
        data = await resp.json()
        assert "poll_interval" in data
