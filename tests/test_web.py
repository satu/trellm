"""Tests for web dashboard server."""

import asyncio
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer

from trellm.config import Config, TrelloConfig, ClaudeConfig, ProjectConfig, WebConfig
from trellm.state import StateManager
from trellm.web.server import WebServer


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
