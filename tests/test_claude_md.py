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

import re
from pathlib import Path

CLAUDE_MD = Path(__file__).parent.parent / "CLAUDE.md"
TESTS_DIR = Path(__file__).parent


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


class TestRetryBackoffGotchaDocumented:
    """The per-card retry / exponential-backoff subsystem (commits
    67002ce, 6281a58, 5d90a67) is a core dispatch mechanism: a
    non-usage-limit failure that exits fast pushes the card into a
    backoff window (30s doubling, capped at 30m) instead of busy-looping
    every poll. CLAUDE.md documented none of it before the 2026-05-17
    maintenance pass — distinct from the *global* usage-limit pause."""

    def test_backoff_concept_is_documented(self):
        content = _content().lower()
        assert "backoff" in content, (
            "CLAUDE.md must document the per-card retry backoff behavior"
        )

    def test_exponential_per_card_backoff_is_documented(self):
        """The load-bearing distinctions: backoff is exponential, and it
        is per-card (not the global pause). Future readers must find
        both without spelunking through __main__.py."""
        content = _content().lower()
        assert "exponential" in content and "per-card" in content, (
            "CLAUDE.md must say the retry backoff is exponential and per-card"
        )


def _documented_test_files() -> set:
    """Test filenames enumerated in CLAUDE.md's Testing section."""
    content = _content()
    idx = content.find("## Testing")
    assert idx != -1, "CLAUDE.md must have a ## Testing section"
    return set(re.findall(r"test_[a-z_]+\.py", content[idx:]))


class TestTestFileListInSync:
    """CLAUDE.md's Testing section enumerates the test files. Successive
    maintenance passes kept finding it stale (test_trello.py listed but
    deleted; five files added but never documented). These tests force
    the documented list to track tests/ so it cannot drift again."""

    def test_documented_files_all_exist(self):
        for name in _documented_test_files():
            assert (TESTS_DIR / name).exists(), (
                f"CLAUDE.md lists {name} but tests/{name} does not exist"
            )

    def test_all_test_files_are_documented(self):
        actual = {p.name for p in TESTS_DIR.glob("test_*.py")}
        missing = actual - _documented_test_files()
        assert not missing, (
            f"CLAUDE.md's Testing section omits: {sorted(missing)}"
        )


class TestReferencedDocsExist:
    """CLAUDE.md names docs/ files as examples of where investigation
    cards land. A stale filename sends readers to a missing file, so
    every lowercase *.md filename CLAUDE.md mentions must resolve to a
    real file (in docs/ or the repo root)."""

    def test_referenced_md_files_exist(self):
        repo_root = TESTS_DIR.parent
        for name in sorted(set(re.findall(r"[a-z0-9-]+\.md", _content()))):
            exists = (repo_root / "docs" / name).exists() or (
                repo_root / name
            ).exists()
            assert exists, (
                f"CLAUDE.md references {name} but it exists nowhere"
            )
