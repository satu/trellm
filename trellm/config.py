"""Configuration loading for TreLLM."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

# Default location of the locally-built patchright-mcp-lite entry point.
# Used when BrowserConfig.patchright_path isn't overridden — see
# docs/patchright-mcp.md §6 / M2.
DEFAULT_PATCHRIGHT_PATH = "~/src/patchright-mcp-lite/dist/index.js"


@dataclass
class TrelloConfig:
    """Trello API configuration."""

    api_key: str
    api_token: str
    board_id: str
    todo_list_id: str
    ready_to_try_list_id: Optional[str] = None
    # Optional: move completed cards to a different board
    done_board_id: Optional[str] = None
    done_list_id: Optional[str] = None
    # Optional: ICE BOX list for maintenance suggestions
    icebox_list_id: Optional[str] = None


@dataclass
class MaintenanceConfig:
    """Maintenance configuration for a project."""

    enabled: bool = False
    interval: int = 10  # Run every N tickets


@dataclass
class BrowserConfig:
    """Controls whether each `claude` subprocess spawn carries
    `--mcp-config <json>` pointing at patchright-mcp-lite.

    Off by default. Opt in once patchright-mcp-lite is built and the host
    browser stack (scripts/start-browser.sh) is reachable on CDP port 9222.
    `patchright_path` lets us point at a non-default checkout (e.g. CI).
    """

    enabled: bool = False
    patchright_path: Optional[str] = None


@dataclass
class ProjectConfig:
    """Per-project configuration."""

    working_dir: str
    session_id: Optional[str] = None
    compact_prompt: Optional[str] = None  # Custom instructions for /compact
    maintenance: Optional[MaintenanceConfig] = None
    aliases: list[str] = field(default_factory=list)  # Short names that map to this project
    # Per-project override of the global browser setting. None = inherit global.
    browser: Optional[BrowserConfig] = None


@dataclass
class ClaudeConfig:
    """Claude Code configuration."""

    binary: str = "claude"
    timeout: int = 1200  # 20 minutes default
    yolo: bool = False  # Run with --dangerously-skip-permissions
    projects: dict[str, ProjectConfig] = field(default_factory=dict)
    # Global maintenance settings (applies to all projects unless overridden)
    maintenance: Optional[MaintenanceConfig] = None
    # Global patchright-mcp browser setting (applies to all projects unless
    # overridden per-project). See BrowserConfig.
    browser: Optional[BrowserConfig] = None


@dataclass
class WebConfig:
    """Web dashboard configuration."""

    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 8077


@dataclass
class Config:
    """Main configuration."""

    trello: TrelloConfig
    claude: ClaudeConfig
    poll_interval: int = 5
    state_file: str = "~/.trellm/state.json"
    web: WebConfig = field(default_factory=WebConfig)

    def get_working_dir(self, project: str) -> Optional[str]:
        """Get working directory for a project."""
        proj = self.claude.projects.get(project)
        return proj.working_dir if proj else None

    def get_initial_session_id(self, project: str) -> Optional[str]:
        """Get initial session ID for a project (from config file)."""
        proj = self.claude.projects.get(project)
        return proj.session_id if proj else None

    def get_compact_prompt(self, project: str) -> Optional[str]:
        """Get custom compaction prompt for a project."""
        proj = self.claude.projects.get(project)
        return proj.compact_prompt if proj else None

    def resolve_project(self, name: str) -> Optional[str]:
        """Resolve a project name or alias to the canonical project name.

        Returns the canonical project name if found (direct match or alias),
        or None if not found.
        """
        # Direct match takes priority
        if name in self.claude.projects:
            return name
        # Check aliases
        for proj_name, proj_config in self.claude.projects.items():
            if name in proj_config.aliases:
                return proj_name
        return None

    def get_all_project_names(self) -> set[str]:
        """Get all valid project names including aliases."""
        names = set(self.claude.projects.keys())
        for proj_config in self.claude.projects.values():
            names.update(proj_config.aliases)
        return names

    def get_maintenance_config(self, project: str) -> Optional[MaintenanceConfig]:
        """Get maintenance configuration for a project.

        Priority:
        1. Per-project maintenance config (if exists)
        2. Global maintenance config (if exists)
        3. None (maintenance disabled)
        """
        proj = self.claude.projects.get(project)
        # Use per-project config if explicitly set
        if proj and proj.maintenance is not None:
            return proj.maintenance
        # Fall back to global config
        return self.claude.maintenance

    def is_browser_enabled(self, project: str) -> bool:
        """Whether the claude subprocess for this project should be spawned
        with `--mcp-config <patchright>`. Per-project override > global > False.
        Absence at both levels (and an unknown project name) yields False
        so existing setups see no behavioural change."""
        proj = self.claude.projects.get(project)
        if proj is not None and proj.browser is not None:
            return proj.browser.enabled
        if self.claude.browser is not None:
            return self.claude.browser.enabled
        return False

    def patchright_mcp_config_json(self) -> str:
        """Build the JSON config blob that `claude --mcp-config <json>` reads
        to discover the patchright MCP server.

        The blob declares a single `patchright` server that runs the local
        patchright-mcp-lite via node, with the CDP endpoint and the browser
        restart command supplied via env vars (consumed by
        patchright-mcp-lite/src/connection.ts).
        """
        patchright_path = (
            self.claude.browser.patchright_path
            if self.claude.browser is not None
            and self.claude.browser.patchright_path
            else DEFAULT_PATCHRIGHT_PATH
        )
        patchright_path = str(Path(patchright_path).expanduser())
        restart_cmd = (
            str(Path(__file__).resolve().parent.parent / "scripts" / "start-browser.sh")
            + " start"
        )
        return json.dumps(
            {
                "mcpServers": {
                    "patchright": {
                        "command": "node",
                        "args": [patchright_path],
                        "env": {
                            "CDP_ENDPOINT": "http://localhost:9222",
                            "BROWSER_RESTART_CMD": restart_cmd,
                        },
                    }
                }
            }
        )


def load_config(config_path: Optional[str] = None) -> Config:
    """Load configuration from file and environment variables.

    Priority for values:
    1. Environment variables (highest)
    2. Config file
    3. Defaults (lowest)
    """
    # Determine config path
    if config_path:
        path = Path(config_path).expanduser()
    else:
        path = Path("~/.trellm/config.yaml").expanduser()

    # Load from file if exists
    data: dict = {}
    if path.exists():
        with open(path) as f:
            data = yaml.safe_load(f) or {}

    # Extract sections
    trello_data = data.get("trello", {})
    claude_data = data.get("claude", {})
    polling_data = data.get("polling", {})
    state_data = data.get("state", {})

    # Build Trello config with env var overrides
    trello = TrelloConfig(
        api_key=os.environ.get("TRELLO_API_KEY", trello_data.get("api_key", "")),
        api_token=os.environ.get("TRELLO_API_TOKEN", trello_data.get("api_token", "")),
        board_id=os.environ.get("TRELLO_BOARD_ID", trello_data.get("board_id", "")),
        todo_list_id=os.environ.get(
            "TRELLO_TODO_LIST_ID", trello_data.get("todo_list_id", "")
        ),
        ready_to_try_list_id=trello_data.get("ready_to_try_list_id"),
        done_board_id=trello_data.get("done_board_id"),
        done_list_id=trello_data.get("done_list_id"),
        icebox_list_id=trello_data.get("icebox_list_id"),
    )

    # Build project configs
    projects: dict[str, ProjectConfig] = {}
    for name, proj_data in claude_data.get("projects", {}).items():
        # Parse maintenance config if present
        maint_data = proj_data.get("maintenance", {})
        maintenance = None
        if maint_data:
            maintenance = MaintenanceConfig(
                enabled=maint_data.get("enabled", False),
                interval=maint_data.get("interval", 10),
            )

        # Parse per-project browser override if present
        proj_browser_data = proj_data.get("browser")
        proj_browser = None
        if proj_browser_data is not None:
            proj_browser = BrowserConfig(
                enabled=proj_browser_data.get("enabled", False),
                patchright_path=proj_browser_data.get("patchright_path"),
            )

        projects[name] = ProjectConfig(
            working_dir=proj_data.get("working_dir", ""),
            session_id=proj_data.get("session_id"),
            compact_prompt=proj_data.get("compact_prompt"),
            maintenance=maintenance,
            aliases=proj_data.get("aliases", []),
            browser=proj_browser,
        )

    # Parse global maintenance config if present
    global_maint_data = claude_data.get("maintenance", {})
    global_maintenance = None
    if global_maint_data:
        global_maintenance = MaintenanceConfig(
            enabled=global_maint_data.get("enabled", False),
            interval=global_maint_data.get("interval", 10),
        )

    # Parse global browser config if present
    global_browser_data = claude_data.get("browser")
    global_browser = None
    if global_browser_data is not None:
        global_browser = BrowserConfig(
            enabled=global_browser_data.get("enabled", False),
            patchright_path=global_browser_data.get("patchright_path"),
        )

    # Build Claude config
    claude = ClaudeConfig(
        binary=claude_data.get("binary", "claude"),
        timeout=claude_data.get("timeout", 1200),
        yolo=claude_data.get("yolo", False),
        projects=projects,
        maintenance=global_maintenance,
        browser=global_browser,
    )

    # Build web config
    web_data = data.get("web", {})
    web = WebConfig(
        enabled=web_data.get("enabled", False),
        host=web_data.get("host", "0.0.0.0"),
        port=web_data.get("port", 8077),
    )

    return Config(
        trello=trello,
        claude=claude,
        poll_interval=polling_data.get("interval_seconds", 5),
        state_file=state_data.get("file", "~/.trellm/state.json"),
        web=web,
    )
