"""Structural checks on CLAUDE.md.

These tests pin three additions made during the 2026-05-12 maintenance
pass (card 6a00fc1977732b8fc7302ca7):

  1. An Entrypoint reference to `start-trellm.sh` under Quick Reference,
     so the canonical Docker-compatible entrypoint is discoverable and
     the obsolete systemd-user-service route is not advertised.
  2. A Gotcha covering the org/monthly-limit global pause introduced in
     commit 6f31e07, so future contributors know cards stay in TODO and
     polling is paused rather than retried on monthly-limit hits.
  3. An Architecture pointer at `docs/` so investigation-style cards
     that produce no code change land their findings somewhere
     discoverable.

The assertions are deliberately loose (substring matches, not exact
strings) so unrelated copy edits don't break the tests. They will only
fail if someone removes the load-bearing concept.
"""

from pathlib import Path

CLAUDE_MD = Path(__file__).parent.parent / "CLAUDE.md"


def _content() -> str:
    return CLAUDE_MD.read_text()


class TestEntrypointDocumented:
    """Quick Reference must point readers at start-trellm.sh — the
    canonical entrypoint used by Docker and direct runs. README already
    documents this; CLAUDE.md must too so contributors don't reach for
    the retired systemd path."""

    def test_start_trellm_sh_is_mentioned(self):
        assert "start-trellm.sh" in _content(), (
            "CLAUDE.md must mention start-trellm.sh as the canonical entrypoint"
        )


class TestMonthlyLimitGotchaDocumented:
    """Gotcha covering the org/monthly-limit global pause from 6f31e07.
    Without this, future contributors will be confused by cards that
    sit in TODO with no error message when the org limit is hit."""

    def test_monthly_limit_concept_is_documented(self):
        content = _content().lower()
        assert "monthly limit" in content or "org limit" in content, (
            "CLAUDE.md must document the org/monthly-limit pause behavior"
        )

    def test_global_pause_behavior_is_documented(self):
        """The non-obvious part is that the pause is global (all
        projects), not per-card or per-project. Future readers must be
        able to find this without spelunking through __main__.py."""
        content = _content().lower()
        assert "global" in content and (
            "pause" in content or "paused" in content
        ), "CLAUDE.md must say the limit-hit pause is global"


class TestDocsFolderPointerDocumented:
    """The `docs/` folder is where investigation cards that produce no
    code land their findings (patchright-mcp.md, prd-web-dashboard.md).
    CLAUDE.md should point readers at it from the Architecture section
    so the pattern is discoverable."""

    def test_docs_folder_is_referenced(self):
        # Look for a reference to the docs/ folder specifically — not
        # just any mention of "docs" or "documentation".
        assert "docs/" in _content(), (
            "CLAUDE.md must reference the docs/ folder for decision notes"
        )
