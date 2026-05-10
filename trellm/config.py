"""Configuration loading for TreLLM."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


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
    """Controls whether the Claude CLI's `--chrome` flag is passed on each
    spawn (so Claude attaches to the headed Chrome via the claude-in-chrome
    extension). Off by default — opt in once the extension is installed."""

    enabled: bool = False


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
    # Global Claude-in-Chrome setting (applies to all projects unless overridden)
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
        """Whether the Claude CLI's `--chrome` flag should be passed for
        this project. Per-project setting overrides the global setting;
        absence at both levels means False."""
        proj = self.claude.projects.get(project)
        if proj is not None and proj.browser is not None:
            return proj.browser.enabled
        if self.claude.browser is not None:
            return self.claude.browser.enabled
        return False

    def is_browser_required_anywhere(self) -> bool:
        """True iff at least one spawn under this config would carry the
        --chrome flag — i.e. global is enabled, or any per-project
        override is enabled. Used by start-trellm.sh at boot to decide
        whether to launch the headed Chrome stack."""
        if self.claude.browser is not None and self.claude.browser.enabled:
            return True
        for proj in self.claude.projects.values():
            if proj.browser is not None and proj.browser.enabled:
                return True
        return False


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
