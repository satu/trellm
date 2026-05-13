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


class TestWebServerCacheControl:
    """Tests for cache-control headers on static files."""

    async def test_static_files_have_no_cache_header(self, client):
        resp = await client.get("/static/app.js")
        assert resp.headers.get("Cache-Control") == "no-cache"

    async def test_index_has_no_cache_header(self, client):
        resp = await client.get("/")
        assert resp.headers.get("Cache-Control") == "no-cache"

    async def test_api_endpoints_no_cache_control(self, client):
        resp = await client.get("/api/status")
        assert "no-cache" not in resp.headers.get("Cache-Control", "")


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

    async def test_tasks_include_retry_state(self, web_server):
        """Running tasks must expose retry counters so the dashboard can
        show 'attempt N' badges. Without this the user can't tell that a
        running task is actually the 5th attempt at the same card."""
        from trellm.__main__ import CardRetryState

        # Seed retry state for a card that's now running again
        web_server.card_retry_state["card-retry-1"] = CardRetryState(
            error_count=3, timeout_count=1, fast_failure_streak=2,
        )
        web_server.track_task(
            "card-retry-1", "testproject", "Stuck card", "https://trello.com/c/x",
        )
        app = web_server._create_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/tasks")
            data = await resp.json()
            task = data["tasks"][0]
            assert task["error_count"] == 3
            assert task["timeout_count"] == 1
            assert task["fast_failure_streak"] == 2


class TestWebServerQueue:
    """Tests for /api/queue — the TODO snapshot used to surface cards
    that are waiting (project busy, or backoff active).

    User ask: 'if there are 2+ cards queueing in TODO for the same
    projects, show me the cards queued and how long they are queueing
    for'."""

    async def test_queue_empty_initially(self, client):
        """Before update_queue is called, /api/queue returns empty."""
        resp = await client.get("/api/queue")
        assert resp.status == 200
        data = await resp.json()
        assert data["queue"] == []

    async def test_queue_reflects_update(self, web_server):
        """update_queue accepts a list of TrelloCard objects and the
        endpoint surfaces them."""
        from trellm.trello import TrelloCard

        web_server.update_queue([
            TrelloCard(
                id="card-a", name="testproject task A", description="",
                url="https://trello.com/c/a",
                last_activity="2026-05-13T05:00:00Z",
            ),
            TrelloCard(
                id="card-b", name="other task B", description="",
                url="https://trello.com/c/b",
                last_activity="2026-05-13T05:30:00Z",
            ),
        ])
        app = web_server._create_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/queue")
            data = await resp.json()
            assert len(data["queue"]) == 2
            ids = [q["card_id"] for q in data["queue"]]
            assert "card-a" in ids
            assert "card-b" in ids

    async def test_queue_marks_running_cards(self, web_server):
        """If a card is currently in _processing_cards, the queue entry
        must show is_running=True so the dashboard can distinguish
        running from waiting."""
        from trellm.trello import TrelloCard

        web_server.processing_cards.add("card-a")
        web_server.update_queue([
            TrelloCard(
                id="card-a", name="testproject task", description="",
                url="https://trello.com/c/a",
                last_activity="2026-05-13T05:00:00Z",
            ),
            TrelloCard(
                id="card-b", name="testproject waiting", description="",
                url="https://trello.com/c/b",
                last_activity="2026-05-13T05:30:00Z",
            ),
        ])
        app = web_server._create_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/queue")
            data = await resp.json()
            by_id = {q["card_id"]: q for q in data["queue"]}
            assert by_id["card-a"]["is_running"] is True
            assert by_id["card-b"]["is_running"] is False

    async def test_queue_includes_retry_state(self, web_server):
        """Cards with accumulated retry state must surface their counters
        and current backoff so the dashboard can show 'errors=3 timeouts=1
        backoff 14m'."""
        import time as time_mod
        from trellm.__main__ import CardRetryState
        from trellm.trello import TrelloCard

        retry = CardRetryState(
            error_count=3, timeout_count=1, fast_failure_streak=2,
        )
        retry.backoff_until = time_mod.time() + 60
        web_server.card_retry_state["card-failing"] = retry

        web_server.update_queue([
            TrelloCard(
                id="card-failing", name="testproject stuck", description="",
                url="https://trello.com/c/x",
                last_activity="2026-05-13T05:00:00Z",
            ),
        ])
        app = web_server._create_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/queue")
            data = await resp.json()
            entry = data["queue"][0]
            assert entry["retry"]["error_count"] == 3
            assert entry["retry"]["timeout_count"] == 1
            assert entry["retry"]["fast_failure_streak"] == 2
            assert 50 <= entry["retry"]["backoff_remaining_seconds"] <= 60

    async def test_queue_resolves_project_via_alias(self, web_server):
        """Card 'tp something' must resolve to 'testproject' (which has
        'tp' as an alias) so the per-project grouping works for aliased
        cards too."""
        from trellm.trello import TrelloCard

        web_server.update_queue([
            TrelloCard(
                id="card-1", name="tp some work", description="",
                url="https://trello.com/c/x",
                last_activity="2026-05-13T05:00:00Z",
            ),
        ])
        app = web_server._create_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/queue")
            data = await resp.json()
            entry = data["queue"][0]
            assert entry["project"] == "testproject"

    async def test_queue_includes_queued_for_seconds(self, web_server):
        """Each entry must expose how long the card has been in TODO,
        derived from its last_activity timestamp."""
        from datetime import datetime, timedelta, timezone
        from trellm.trello import TrelloCard

        ten_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        # Replace +00:00 with Z to match Trello's format (handler must accept either)
        ten_min_ago_z = ten_min_ago.replace("+00:00", "Z")
        web_server.update_queue([
            TrelloCard(
                id="card-q", name="testproject queued", description="",
                url="https://trello.com/c/q",
                last_activity=ten_min_ago_z,
            ),
        ])
        app = web_server._create_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/queue")
            data = await resp.json()
            queued = data["queue"][0]["queued_for_seconds"]
            # Allow ~5s of slop for test execution time
            assert 595 <= queued <= 615


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

    async def test_index_links_pwa_manifest(self, client):
        resp = await client.get("/")
        text = await resp.text()
        assert 'rel="manifest"' in text
        assert "/static/manifest.webmanifest" in text


class TestWebServerPWAAssets:
    """Tests for PWA manifest and icon assets."""

    async def test_manifest_served_with_correct_mime(self, client):
        resp = await client.get("/static/manifest.webmanifest")
        assert resp.status == 200
        # Browsers require application/manifest+json (or json) for manifests.
        assert resp.headers.get("Content-Type", "").startswith(
            ("application/manifest+json", "application/json")
        )
        body = await resp.json()
        assert body["name"] == "TreLLM Dashboard"
        purposes = {icon["purpose"] for icon in body["icons"]}
        assert "any" in purposes
        assert "maskable" in purposes
        for icon in body["icons"]:
            assert icon["src"].startswith("/static/icons/")

    @pytest.mark.parametrize("path", [
        "/static/icons/icon-192.png",
        "/static/icons/icon-512.png",
        "/static/icons/icon-maskable-192.png",
        "/static/icons/icon-maskable-512.png",
        "/static/icons/apple-touch-icon.png",
        "/static/icons/favicon-32.png",
    ])
    async def test_icon_assets_are_served(self, client, path):
        resp = await client.get(path)
        assert resp.status == 200
        assert resp.headers.get("Content-Type") == "image/png"
        body = await resp.read()
        # PNG signature
        assert body[:8] == b"\x89PNG\r\n\x1a\n"

    async def test_maskable_icon_has_safe_zone_padding(self):
        """The Android maskable icon must keep its design inside the inner
        80% safe zone so adaptive masks can crop without losing content.

        Verifies that the corners and edge centres of the committed
        ``icon-maskable-512.png`` are pure brand background — i.e. the
        squircle does NOT bleed into the safe-zone margin.
        """
        Image = pytest.importorskip("PIL.Image")
        from pathlib import Path
        from PIL import Image as I  # noqa: F811

        path = Path(__file__).resolve().parent.parent \
            / "trellm/web/static/icons/icon-maskable-512.png"
        im = I.open(path).convert("RGBA")
        assert im.size == (512, 512)

        brand_bg = (15, 17, 23, 255)
        # Outer 10% should be solid brand background — sample 8 points.
        margin = 16
        samples = [
            (margin, margin),                  # TL corner
            (512 - margin, margin),            # TR corner
            (margin, 512 - margin),            # BL corner
            (512 - margin, 512 - margin),      # BR corner
            (256, margin),                     # top edge
            (256, 512 - margin),               # bottom edge
            (margin, 256),                     # left edge
            (512 - margin, 256),               # right edge
        ]
        for x, y in samples:
            assert im.getpixel((x, y)) == brand_bg, (
                f"maskable icon bleeds into safe-zone margin at ({x},{y}) "
                f"got {im.getpixel((x, y))}"
            )
        # Centre is design (not background).
        assert im.getpixel((256, 256)) != brand_bg


class TestDashboardJSSafety:
    """Regression tests for the dashboard JS button rendering.

    The original bug: card names containing a literal ``"`` (the bug
    report card itself was titled ``... clicking on "view" on the
    dashboard ...``) were interpolated raw into ``onclick="viewOutput(...)"``
    attributes, terminating the attribute mid-value and silently breaking
    the click handler. Card titles come from Trello and are arbitrary
    user input, so the dashboard must not interpolate them into HTML
    attribute values.
    """

    async def test_view_buttons_use_data_attributes_not_inline_onclick(self, client):
        resp = await client.get("/static/app.js")
        body = await resp.text()
        # Inline onclick handlers that interpolate card_name break for any
        # title containing ``"`` — switched to data-* attributes + delegated
        # click listeners.
        assert 'onclick="viewOutput(' not in body, (
            "View button still uses inline onclick — breaks for card "
            "names containing double quotes"
        )
        assert 'onclick="viewCompletedOutput(' not in body, (
            "Completed View button still uses inline onclick"
        )
        # Positive marker that the new event-delegation pattern is in place.
        assert "data-card-id" in body

    async def test_escape_html_escapes_double_quotes(self, client):
        """``escapeHtml`` is now used for HTML attribute values, so it
        must escape ``"`` (otherwise card names with quotes still corrupt
        ``data-card-name="..."`` attributes)."""
        resp = await client.get("/static/app.js")
        body = await resp.text()
        # Either an explicit ``"`` -> ``&quot;`` (string-replace style) or
        # a documented attribute-safe escape — both forms count.
        assert "&quot;" in body, (
            "escapeHtml does not appear to escape \" — attribute injection "
            "via card names containing double quotes is still possible"
        )


class TestPullToRefresh:
    """Tests for the iOS-standalone pull-to-refresh hack."""

    async def test_index_loads_pull_to_refresh_script(self, client):
        resp = await client.get("/")
        text = await resp.text()
        assert "/static/pull-to-refresh.js" in text

    async def test_pull_to_refresh_script_is_served(self, client):
        resp = await client.get("/static/pull-to-refresh.js")
        assert resp.status == 200
        body = await resp.text()
        # Gated on iOS standalone — must check navigator.standalone so the
        # hack stays out of the way on Android Chrome / desktop where native
        # pull-to-refresh already works.
        assert "navigator.standalone" in body
        # Triggers a reload when the user pulls past the threshold.
        assert "location.reload" in body

    async def test_index_has_ios_standalone_meta_tags(self, client):
        """iOS needs apple-mobile-web-app-capable for navigator.standalone
        to ever become true, otherwise the PTR hack never activates."""
        resp = await client.get("/")
        text = await resp.text()
        assert 'name="apple-mobile-web-app-capable"' in text
        assert 'content="yes"' in text
        assert "apple-mobile-web-app-status-bar-style" in text


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

    async def test_usage_refresh_error_does_not_start_cooldown(self, web_server):
        """If the API returns an error (e.g. 429), cooldown should NOT start."""
        call_count = 0

        def error_then_success():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ClaudeUsageLimits(error="API error: 429")
            return _mock_usage_limits()

        with patch("trellm.web.server.fetch_claude_usage_limits", side_effect=error_then_success):
            await web_server.refresh_usage_limits()
            assert call_count == 1
            # Second call should NOT be skipped because the first was an error
            await web_server.refresh_usage_limits()
            assert call_count == 2

    async def test_manual_refresh_also_respects_cooldown(self, web_server):
        """Manual refresh button must also respect the 5-minute cooldown."""
        call_count = 0
        original_limits = _mock_usage_limits()

        def counting_fetch():
            nonlocal call_count
            call_count += 1
            return original_limits

        app = web_server._create_app()
        with patch("trellm.web.server.fetch_claude_usage_limits", side_effect=counting_fetch):
            async with TestClient(TestServer(app)) as client:
                # First refresh succeeds
                await client.post("/api/usage/refresh")
                assert call_count == 1
                # Second refresh within cooldown should be skipped
                await client.post("/api/usage/refresh")
                assert call_count == 1

    async def test_stats_includes_cache_age(self, web_server):
        """Stats endpoint should include cache age in seconds."""
        app = web_server._create_app()
        async with TestClient(TestServer(app)) as client:
            # Refresh to populate cache
            await client.post("/api/usage/refresh")
            resp = await client.get("/api/stats")
            data = await resp.json()
            assert "usage_cache_age_seconds" in data
            assert isinstance(data["usage_cache_age_seconds"], int)
            assert data["usage_cache_age_seconds"] >= 0


    async def test_usage_cache_persists_across_restart(self, config, tmp_path):
        """Usage cache should persist in state file and survive restarts."""
        state = _make_state(tmp_path)
        ws1 = _make_web_server(config, state)

        # First server fetches usage data
        with patch("trellm.web.server.fetch_claude_usage_limits", return_value=_mock_usage_limits()):
            await ws1.refresh_usage_limits()

        assert ws1._usage_cache is not None
        assert ws1._usage_cache_time > 0

        # Create a new WebServer simulating restart (same state file)
        ws2 = _make_web_server(config, state)
        # Should load cached data from state
        assert ws2._usage_cache is not None
        assert ws2._usage_cache_time > 0
        assert ws2._usage_cache.get("five_hour", {}).get("utilization") == 42.5

    async def test_usage_cache_cooldown_survives_restart(self, config, tmp_path):
        """Cooldown should be respected even after restart."""
        state = _make_state(tmp_path)
        ws1 = _make_web_server(config, state)
        call_count = 0

        def counting_fetch():
            nonlocal call_count
            call_count += 1
            return _mock_usage_limits()

        with patch("trellm.web.server.fetch_claude_usage_limits", side_effect=counting_fetch):
            await ws1.refresh_usage_limits()
            assert call_count == 1

        # Create new server (restart) using same state
        ws2 = _make_web_server(config, state)
        with patch("trellm.web.server.fetch_claude_usage_limits", side_effect=counting_fetch):
            await ws2.refresh_usage_limits()
            # Should be skipped due to cooldown persisted in state
            assert call_count == 1


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

    def test_output_preserved_on_untrack(self, web_server):
        """Output is preserved in completed tasks after untrack."""
        web_server.track_task("card1", "proj", "test card", "http://example.com")
        web_server.append_output("card1", "line 1\n")
        web_server.untrack_task("card1")
        # Output still accessible via get_output (from completed tasks)
        assert web_server.get_output("card1") == ["line 1\n"]

    def test_output_buffer_limit(self, web_server):
        web_server.track_task("card1", "proj", "test card", "http://example.com")
        # Write more lines than the buffer limit
        for i in range(6000):
            web_server.append_output("card1", f"line {i}\n")
        lines = web_server.get_output("card1")
        assert len(lines) <= 5000
        # Should keep the most recent lines
        assert lines[-1] == "line 5999\n"


class TestWebServerCompletedTasks:
    """Tests for completed task retention."""

    def test_untrack_preserves_output_in_completed(self, web_server):
        web_server.track_task("card1", "proj", "test card", "http://example.com")
        web_server.append_output("card1", "line 1\n")
        web_server.append_output("card1", "line 2\n")
        web_server.untrack_task("card1")
        # Output should be preserved in completed tasks
        completed = web_server.get_completed_tasks()
        assert len(completed) == 1
        assert completed[0]["card_id"] == "card1"
        assert completed[0]["output_lines"] == 2

    def test_completed_tasks_limited_to_10(self, web_server):
        for i in range(15):
            cid = f"card{i}"
            web_server.track_task(cid, "proj", f"card {i}", f"http://example.com/{i}")
            web_server.append_output(cid, f"output {i}\n")
            web_server.untrack_task(cid)
        completed = web_server.get_completed_tasks()
        assert len(completed) == 10
        # Should keep the most recent
        assert completed[0]["card_id"] == "card14"

    def test_same_card_running_and_completed_output_isolated(self, web_server):
        """When a card has both a completed run and a new running run,
        the completed output should be separate from the running output."""
        # First run completes
        web_server.track_task("card1", "proj", "test card", "http://example.com")
        web_server.append_output("card1", "first run output\n")
        web_server.untrack_task("card1")

        # Same card starts a new run
        web_server.track_task("card1", "proj", "test card", "http://example.com")
        web_server.append_output("card1", "second run output\n")

        # Completed tasks should have a run_id to access completed output
        completed = web_server.get_completed_tasks()
        assert len(completed) == 1
        run_id = completed[0]["run_id"]
        assert run_id != "card1"  # run_id should be different from card_id

        # get_output with run_id should return completed output
        completed_output = web_server.get_output(run_id)
        assert "first run output\n" in completed_output
        assert "second run output\n" not in completed_output

        # get_output with card_id should return running task output
        running_output = web_server.get_output("card1")
        assert "second run output\n" in running_output

    def test_completed_task_output_accessible(self, web_server):
        web_server.track_task("card1", "proj", "test", "http://example.com")
        web_server.append_output("card1", "hello\n")
        web_server.untrack_task("card1")
        output = web_server.get_output("card1")
        assert output == ["hello\n"]

    def test_failed_task_not_in_completed_list(self, web_server):
        """Tasks that finish with success=False (e.g. org-limit failures)
        should NOT appear in the recent-completions list — that list was
        getting flooded with the same card 30+ times when busy-looping
        on org limit errors."""
        web_server.track_task("card1", "proj", "test", "http://example.com")
        web_server.append_output("card1", "claude failed\n")
        web_server.untrack_task("card1", success=False)
        completed = web_server.get_completed_tasks()
        assert completed == []

    def test_failed_task_signals_subscribers(self, web_server):
        """Failed runs must still wake up SSE subscribers so the stream
        endpoint can close the connection cleanly."""
        web_server.track_task("card1", "proj", "test", "http://example.com")
        queue: asyncio.Queue = asyncio.Queue()
        web_server._task_output_subscribers["card1"].append(queue)

        web_server.untrack_task("card1", success=False)

        # Subscriber should have received the None sentinel
        assert queue.qsize() == 1
        assert queue.get_nowait() is None

    @pytest.mark.asyncio
    async def test_completed_tasks_api_endpoint(self, web_server):
        web_server.track_task("card1", "proj", "test card", "http://example.com")
        web_server.append_output("card1", "output line\n")
        web_server.untrack_task("card1")
        app = web_server._create_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/completed")
            assert resp.status == 200
            data = await resp.json()
            assert len(data["completed"]) == 1
            assert data["completed"][0]["card_name"] == "test card"
            assert data["completed"][0]["output_lines"] == 1

    @pytest.mark.asyncio
    async def test_completed_includes_tokens_from_history(self, web_server):
        """Recent Completions should surface tokens for each finished card
        so users don't need a separate history view to see usage."""
        web_server.state.record_cost(
            card_id="card1",
            project="proj",
            total_cost="$1.00",
            api_duration="1m",
            wall_duration="2m",
            code_changes="+10 -5",
            input_tokens=12345,
            output_tokens=678,
        )
        web_server.track_task("card1", "proj", "test card", "http://example.com")
        web_server.append_output("card1", "line\n")
        web_server.untrack_task("card1")
        app = web_server._create_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/completed")
            data = await resp.json()
            assert data["completed"][0]["input_tokens"] == 12345
            assert data["completed"][0]["output_tokens"] == 678

    @pytest.mark.asyncio
    async def test_completed_tokens_zero_without_history(self, web_server):
        """Falls back to zero tokens when no record_cost was made — keeps
        the API contract uniform regardless of whether stats landed."""
        web_server.track_task("card1", "proj", "test card", "http://example.com")
        web_server.append_output("card1", "line\n")
        web_server.untrack_task("card1")
        app = web_server._create_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/completed")
            data = await resp.json()
            assert data["completed"][0]["input_tokens"] == 0
            assert data["completed"][0]["output_tokens"] == 0

    @pytest.mark.asyncio
    async def test_completed_uses_most_recent_history_for_card(self, web_server):
        """If a card was retried, the most recent ticket_history entry for
        that card_id is what users care about (earlier failed runs may have
        smaller token counts)."""
        web_server.state.record_cost(
            card_id="card1",
            project="proj",
            total_cost="$0.10",
            api_duration="10s",
            wall_duration="20s",
            code_changes="",
            input_tokens=100,
            output_tokens=10,
        )
        web_server.state.record_cost(
            card_id="card1",
            project="proj",
            total_cost="$1.00",
            api_duration="1m",
            wall_duration="2m",
            code_changes="+1 -1",
            input_tokens=9999,
            output_tokens=888,
        )
        web_server.track_task("card1", "proj", "test card", "http://example.com")
        web_server.append_output("card1", "line\n")
        web_server.untrack_task("card1")
        app = web_server._create_app()
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/completed")
            data = await resp.json()
            assert data["completed"][0]["input_tokens"] == 9999
            assert data["completed"][0]["output_tokens"] == 888


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
    async def test_stream_completed_via_run_id_while_same_card_running(self, web_server):
        """When a card has both a completed run and a new running run,
        streaming the completed run's output via run_id should return
        the completed output, not the running task's output."""
        app = web_server._create_app()
        async with TestClient(TestServer(app)) as client:
            # First run completes
            web_server.track_task("card1", "proj", "test", "http://example.com")
            web_server.append_output("card1", "completed output\n")
            web_server.untrack_task("card1")

            # Same card starts a new run
            web_server.track_task("card1", "proj", "test", "http://example.com")
            web_server.append_output("card1", "running output\n")

            # Get the run_id of the completed task
            completed = web_server.get_completed_tasks()
            assert len(completed) == 1
            run_id = completed[0]["run_id"]

            # Stream completed output via run_id
            resp = await client.get(f"/api/stream/{run_id}")
            data = await resp.content.read(4096)
            text = data.decode()

            # Should contain completed output, not running output
            assert "completed output" in text
            assert "running output" not in text
            # Should contain done event (completed task)
            assert "event: done" in text

    @pytest.mark.asyncio
    async def test_stream_completed_via_card_id_while_same_card_running(self, web_server):
        """When viewing completed output via card_id (e.g. from cached JS),
        and the same card is also running, the stream should serve the
        running task's output (not mix with completed)."""
        app = web_server._create_app()
        async with TestClient(TestServer(app)) as client:
            # First run completes
            web_server.track_task("card1", "proj", "test", "http://example.com")
            web_server.append_output("card1", "completed output\n")
            web_server.untrack_task("card1")

            # Same card starts a new run
            web_server.track_task("card1", "proj", "test", "http://example.com")
            web_server.append_output("card1", "running output\n")

            # Stream via card_id (running task takes precedence)
            resp = await client.get("/api/stream/card1")
            data = await resp.content.read(4096)
            text = data.decode()

            # Should contain running output
            assert "running output" in text

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
