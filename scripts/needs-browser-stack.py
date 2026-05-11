#!/usr/bin/env python3
"""Exit 0 if the trellm config indicates the patchright browser stack
should be auto-started, exit 1 otherwise.

Used by start-trellm.sh to decide whether to bring up
scripts/start-browser.sh before launching trellm. The decision is:

    auto-start := claude.browser.enabled (global) is True
                  OR any claude.projects.<name>.browser.enabled is True

A missing browser block at all levels yields exit 1 (no auto-start),
matching the M2 default of "browser is opt-in".

Usage:
    scripts/needs-browser-stack.py [config-path]

If `config-path` is omitted, reads from ~/.trellm/config.yaml.
"""

import sys
from pathlib import Path

# Make trellm importable when run from a checkout without installation
# (e.g. from start-trellm.sh before `pip install -e .` runs — though
# in practice start-trellm.sh runs the install first).
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from trellm.config import load_config  # noqa: E402


def needs_browser_stack(config_path: str | None = None) -> bool:
    config = load_config(config_path)
    if config.claude.browser is not None and config.claude.browser.enabled:
        return True
    for proj in config.claude.projects.values():
        if proj.browser is not None and proj.browser.enabled:
            return True
    return False


def main() -> int:
    config_path = sys.argv[1] if len(sys.argv) > 1 else None
    return 0 if needs_browser_stack(config_path) else 1


if __name__ == "__main__":
    sys.exit(main())
