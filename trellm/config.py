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


@dataclass
class ProjectConfig:
    """Per-project configuration."""

    working_dir: str
    session_id: Optional[str] = None


@dataclass
class ClaudeConfig:
    """Claude Code configuration."""

    binary: str = "claude"
    timeout: int = 600  # 10 minutes default
    projects: dict[str, ProjectConfig] = field(default_factory=dict)


@dataclass
class Config:
    """Main configuration."""

    trello: TrelloConfig
    claude: ClaudeConfig
    poll_interval: int = 5
    state_file: str = "~/.trellm/state.json"

    def get_working_dir(self, project: str) -> Optional[str]:
        """Get working directory for a project."""
        proj = self.claude.projects.get(project)
        return proj.working_dir if proj else None

    def get_initial_session_id(self, project: str) -> Optional[str]:
        """Get initial session ID for a project (from config file)."""
        proj = self.claude.projects.get(project)
        return proj.session_id if proj else None


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
    )

    # Build project configs
    projects: dict[str, ProjectConfig] = {}
    for name, proj_data in claude_data.get("projects", {}).items():
        projects[name] = ProjectConfig(
            working_dir=proj_data.get("working_dir", ""),
            session_id=proj_data.get("session_id"),
        )

    # Build Claude config
    claude = ClaudeConfig(
        binary=claude_data.get("binary", "claude"),
        timeout=claude_data.get("timeout", 600),
        projects=projects,
    )

    return Config(
        trello=trello,
        claude=claude,
        poll_interval=polling_data.get("interval_seconds", 5),
        state_file=state_data.get("file", "~/.trellm/state.json"),
    )
