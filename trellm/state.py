"""State persistence for TreLLM."""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class TicketStats:
    """Statistics for a single ticket/card."""

    card_id: str
    project: str
    cost_cents: int
    api_duration_seconds: int
    wall_duration_seconds: int
    lines_added: int
    lines_removed: int
    processed_at: str


@dataclass
class AggregatedStats:
    """Aggregated statistics for display."""

    total_cost_cents: int = 0
    total_tickets: int = 0
    total_api_duration_seconds: int = 0
    total_wall_duration_seconds: int = 0
    total_lines_added: int = 0
    total_lines_removed: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_creation_tokens: int = 0
    total_cache_read_tokens: int = 0

    @property
    def average_cost_cents(self) -> float:
        """Average cost per ticket in cents."""
        return self.total_cost_cents / self.total_tickets if self.total_tickets > 0 else 0

    @property
    def total_cost_dollars(self) -> str:
        """Total cost formatted as dollars."""
        return f"${self.total_cost_cents / 100:.2f}"

    @property
    def average_cost_dollars(self) -> str:
        """Average cost per ticket formatted as dollars."""
        return f"${self.average_cost_cents / 100:.2f}"

    def format_duration(self, seconds: int) -> str:
        """Format duration in human-readable format."""
        if seconds < 60:
            return f"{seconds}s"
        elif seconds < 3600:
            minutes = seconds // 60
            secs = seconds % 60
            return f"{minutes}m {secs}s"
        else:
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            return f"{hours}h {minutes}m"

    @property
    def api_duration_formatted(self) -> str:
        """API duration in human-readable format."""
        return self.format_duration(self.total_api_duration_seconds)

    @property
    def wall_duration_formatted(self) -> str:
        """Wall duration in human-readable format."""
        return self.format_duration(self.total_wall_duration_seconds)

    def format_tokens(self, tokens: int) -> str:
        """Format token count in human-readable format (K, M suffixes)."""
        if tokens < 1000:
            return str(tokens)
        elif tokens < 1_000_000:
            return f"{tokens / 1000:.1f}K"
        else:
            return f"{tokens / 1_000_000:.2f}M"

    @property
    def total_tokens(self) -> int:
        """Total tokens (input + output)."""
        return self.total_input_tokens + self.total_output_tokens

    @property
    def total_tokens_formatted(self) -> str:
        """Total tokens in human-readable format."""
        return self.format_tokens(self.total_tokens)

    @property
    def input_tokens_formatted(self) -> str:
        """Input tokens in human-readable format."""
        return self.format_tokens(self.total_input_tokens)

    @property
    def output_tokens_formatted(self) -> str:
        """Output tokens in human-readable format."""
        return self.format_tokens(self.total_output_tokens)

    @property
    def cache_read_tokens_formatted(self) -> str:
        """Cache read tokens in human-readable format."""
        return self.format_tokens(self.total_cache_read_tokens)


class StateManager:
    """Manages persistent state for TreLLM.

    State includes:
    - Session IDs per project (for Claude Code --resume)
    - Processed card IDs with timestamps
    """

    def __init__(self, state_file: str):
        self.path = Path(state_file).expanduser()
        self.state = self._load()

    def _load(self) -> dict:
        """Load state from file."""
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                # Ensure stats structure exists
                if "stats" not in data:
                    data["stats"] = self._empty_stats()
                return data
            except Exception as e:
                logger.error("Failed to load state from %s: %s", self.path, e)
        return {"sessions": {}, "processed": {}, "stats": self._empty_stats()}

    def _empty_stats(self) -> dict:
        """Return empty stats structure."""
        return {
            "global": {
                "total_cost_cents": 0,
                "total_tickets": 0,
                "total_api_duration_seconds": 0,
                "total_wall_duration_seconds": 0,
                "total_lines_added": 0,
                "total_lines_removed": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_cache_creation_tokens": 0,
                "total_cache_read_tokens": 0,
            },
            "by_project": {},
            "by_date": {},
            "ticket_history": [],
        }

    def _save(self) -> None:
        """Save state to file, running rollup to keep data compact."""
        self._rollup_old_dates()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state, indent=2))

    def _rollup_old_dates(self) -> None:
        """Roll up old date entries to reduce storage size.

        RRDTool-style compaction strategy:
        - Days 1-30: Keep daily granularity
        - Days 31-90: Aggregate into weekly buckets (week-YYYY-WW)
        - Days 91+: Aggregate into monthly buckets (month-YYYY-MM)
        """
        if "stats" not in self.state or "by_date" not in self.state["stats"]:
            return

        by_date = self.state["stats"]["by_date"]
        today = datetime.now(timezone.utc).date()

        # Separate entries into daily, weekly, and monthly
        to_remove: list[str] = []
        weekly_buckets: dict[str, dict] = {}
        monthly_buckets: dict[str, dict] = {}

        for date_key, stats in list(by_date.items()):
            # Skip already-rolled-up entries (week-* and month-*)
            if date_key.startswith("week-") or date_key.startswith("month-"):
                continue

            try:
                entry_date = datetime.strptime(date_key, "%Y-%m-%d").date()
            except ValueError:
                continue

            days_old = (today - entry_date).days

            if days_old <= 30:
                # Keep daily entries for last 30 days
                continue
            elif days_old <= 90:
                # Aggregate into weekly buckets
                # Use ISO week number for consistent week boundaries
                year, week, _ = entry_date.isocalendar()
                bucket_key = f"week-{year}-{week:02d}"

                if bucket_key not in weekly_buckets:
                    weekly_buckets[bucket_key] = self._empty_bucket()

                self._aggregate_into_bucket(weekly_buckets[bucket_key], stats)
                to_remove.append(date_key)
            else:
                # Aggregate into monthly buckets
                bucket_key = f"month-{entry_date.year}-{entry_date.month:02d}"

                if bucket_key not in monthly_buckets:
                    monthly_buckets[bucket_key] = self._empty_bucket()

                self._aggregate_into_bucket(monthly_buckets[bucket_key], stats)
                to_remove.append(date_key)

        # Also roll up old weekly buckets into monthly
        for date_key, stats in list(by_date.items()):
            if not date_key.startswith("week-"):
                continue

            # Parse week-YYYY-WW
            try:
                parts = date_key.split("-")
                year = int(parts[1])
                week = int(parts[2])
                # Get first day of that ISO week
                week_start = datetime.strptime(f"{year}-W{week:02d}-1", "%Y-W%W-%w").date()
            except (ValueError, IndexError):
                continue

            days_old = (today - week_start).days

            if days_old > 90:
                # Roll weekly into monthly
                bucket_key = f"month-{week_start.year}-{week_start.month:02d}"

                if bucket_key not in monthly_buckets:
                    monthly_buckets[bucket_key] = self._empty_bucket()

                self._aggregate_into_bucket(monthly_buckets[bucket_key], stats)
                to_remove.append(date_key)

        # Apply changes
        for key in to_remove:
            if key in by_date:
                del by_date[key]

        # Add new weekly and monthly buckets
        for bucket_key, bucket_stats in weekly_buckets.items():
            if bucket_key in by_date:
                # Merge with existing
                self._aggregate_into_bucket(by_date[bucket_key], bucket_stats)
            else:
                by_date[bucket_key] = bucket_stats

        for bucket_key, bucket_stats in monthly_buckets.items():
            if bucket_key in by_date:
                # Merge with existing
                self._aggregate_into_bucket(by_date[bucket_key], bucket_stats)
            else:
                by_date[bucket_key] = bucket_stats

        if to_remove:
            logger.debug("Rolled up %d old date entries", len(to_remove))

    def _empty_bucket(self) -> dict:
        """Return an empty stats bucket."""
        return {
            "total_cost_cents": 0,
            "total_tickets": 0,
            "total_api_duration_seconds": 0,
            "total_wall_duration_seconds": 0,
            "total_lines_added": 0,
            "total_lines_removed": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cache_creation_tokens": 0,
            "total_cache_read_tokens": 0,
        }

    def _aggregate_into_bucket(self, bucket: dict, stats: dict) -> None:
        """Aggregate stats into a bucket."""
        bucket["total_cost_cents"] += stats.get("total_cost_cents", 0)
        bucket["total_tickets"] += stats.get("total_tickets", 0)
        bucket["total_api_duration_seconds"] += stats.get("total_api_duration_seconds", 0)
        bucket["total_wall_duration_seconds"] += stats.get("total_wall_duration_seconds", 0)
        bucket["total_lines_added"] += stats.get("total_lines_added", 0)
        bucket["total_lines_removed"] += stats.get("total_lines_removed", 0)
        bucket["total_input_tokens"] += stats.get("total_input_tokens", 0)
        bucket["total_output_tokens"] += stats.get("total_output_tokens", 0)
        bucket["total_cache_creation_tokens"] += stats.get("total_cache_creation_tokens", 0)
        bucket["total_cache_read_tokens"] += stats.get("total_cache_read_tokens", 0)

    def get_session(self, project: str) -> Optional[str]:
        """Get session ID for a project."""
        session = self.state.get("sessions", {}).get(project)
        return session.get("session_id") if session else None

    def set_session(
        self, project: str, session_id: str, last_card_id: Optional[str] = None
    ) -> None:
        """Store session ID for a project.

        Args:
            project: Project name
            session_id: The Claude Code session ID
            last_card_id: Optional card ID of the last processed card
        """
        session_data = self.state.setdefault("sessions", {}).setdefault(project, {})
        session_data["session_id"] = session_id
        session_data["last_activity"] = datetime.now(timezone.utc).isoformat()
        if last_card_id:
            session_data["last_card_id"] = last_card_id
        self._save()

    def get_last_card_id(self, project: str) -> Optional[str]:
        """Get the last processed card ID for a project."""
        session = self.state.get("sessions", {}).get(project)
        return session.get("last_card_id") if session else None

    def is_processed(self, card_id: str) -> bool:
        """Check if a card has been processed."""
        return card_id in self.state.get("processed", {})

    def should_reprocess(self, card_id: str, last_activity: str) -> bool:
        """Check if a card should be reprocessed (moved back to TODO).

        A card should be reprocessed if it was previously processed but
        has been modified since (e.g., moved back to TODO with new feedback).
        """
        processed = self.state.get("processed", {}).get(card_id)
        if not processed:
            return False
        return last_activity > processed.get("processed_at", "")

    def mark_processed(self, card_id: str) -> None:
        """Mark a card as processed."""
        self.state.setdefault("processed", {})[card_id] = {
            "processed_at": datetime.now(timezone.utc).isoformat(),
            "status": "complete",
        }
        self._save()

    def clear_processed(self, card_id: str) -> None:
        """Clear processed status for a card (for reprocessing)."""
        if card_id in self.state.get("processed", {}):
            del self.state["processed"][card_id]
            self._save()

    def _parse_cost(self, cost_str: Optional[str]) -> int:
        """Parse cost string (e.g., '$1.23') to cents."""
        if not cost_str:
            return 0
        # Remove $ and convert to cents
        match = re.search(r"\$?(\d+)\.(\d{2})", cost_str)
        if match:
            dollars = int(match.group(1))
            cents = int(match.group(2))
            return dollars * 100 + cents
        # Try integer cents
        match = re.search(r"(\d+)\s*cents?", cost_str, re.IGNORECASE)
        if match:
            return int(match.group(1))
        return 0

    def _parse_duration(self, duration_str: Optional[str]) -> int:
        """Parse duration string (e.g., '2m 30s', '1h 5m') to seconds."""
        if not duration_str:
            return 0
        total_seconds = 0
        # Match hours
        match = re.search(r"(\d+)\s*h", duration_str, re.IGNORECASE)
        if match:
            total_seconds += int(match.group(1)) * 3600
        # Match minutes
        match = re.search(r"(\d+)\s*m(?:in)?", duration_str, re.IGNORECASE)
        if match:
            total_seconds += int(match.group(1)) * 60
        # Match seconds
        match = re.search(r"(\d+)\s*s(?:ec)?", duration_str, re.IGNORECASE)
        if match:
            total_seconds += int(match.group(1))
        return total_seconds

    def _parse_code_changes(self, changes_str: Optional[str]) -> tuple[int, int]:
        """Parse code changes string (e.g., '+500 -200') to (added, removed)."""
        if not changes_str:
            return 0, 0
        added = 0
        removed = 0
        # Match +N
        match = re.search(r"\+(\d+)", changes_str)
        if match:
            added = int(match.group(1))
        # Match -N
        match = re.search(r"-(\d+)", changes_str)
        if match:
            removed = int(match.group(1))
        return added, removed

    def record_cost(
        self,
        card_id: str,
        project: str,
        total_cost: Optional[str] = None,
        api_duration: Optional[str] = None,
        wall_duration: Optional[str] = None,
        code_changes: Optional[str] = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> None:
        """Record cost and usage statistics for a completed ticket.

        Args:
            card_id: The Trello card ID
            project: Project name
            total_cost: Cost string (e.g., '$1.23')
            api_duration: API duration string (e.g., '2m 30s')
            wall_duration: Wall duration string (e.g., '5m 15s')
            code_changes: Code changes string (e.g., '+500 -200')
            input_tokens: Number of input tokens used
            output_tokens: Number of output tokens used
            cache_creation_tokens: Number of cache creation tokens
            cache_read_tokens: Number of cache read tokens
        """
        # Parse values
        cost_cents = self._parse_cost(total_cost)
        api_seconds = self._parse_duration(api_duration)
        wall_seconds = self._parse_duration(wall_duration)
        lines_added, lines_removed = self._parse_code_changes(code_changes)

        # Get current date for daily aggregation
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        now = datetime.now(timezone.utc).isoformat()

        # Ensure stats structure exists
        if "stats" not in self.state:
            self.state["stats"] = self._empty_stats()

        stats = self.state["stats"]

        # Update global stats
        stats["global"]["total_cost_cents"] += cost_cents
        stats["global"]["total_tickets"] += 1
        stats["global"]["total_api_duration_seconds"] += api_seconds
        stats["global"]["total_wall_duration_seconds"] += wall_seconds
        stats["global"]["total_lines_added"] += lines_added
        stats["global"]["total_lines_removed"] += lines_removed
        # Initialize token fields if they don't exist (backward compat)
        stats["global"].setdefault("total_input_tokens", 0)
        stats["global"].setdefault("total_output_tokens", 0)
        stats["global"].setdefault("total_cache_creation_tokens", 0)
        stats["global"].setdefault("total_cache_read_tokens", 0)
        stats["global"]["total_input_tokens"] += input_tokens
        stats["global"]["total_output_tokens"] += output_tokens
        stats["global"]["total_cache_creation_tokens"] += cache_creation_tokens
        stats["global"]["total_cache_read_tokens"] += cache_read_tokens

        # Update per-project stats
        if project not in stats["by_project"]:
            stats["by_project"][project] = self._empty_bucket()
        proj_stats = stats["by_project"][project]
        # Initialize token fields if they don't exist (backward compat)
        proj_stats.setdefault("total_input_tokens", 0)
        proj_stats.setdefault("total_output_tokens", 0)
        proj_stats.setdefault("total_cache_creation_tokens", 0)
        proj_stats.setdefault("total_cache_read_tokens", 0)
        proj_stats["total_cost_cents"] += cost_cents
        proj_stats["total_tickets"] += 1
        proj_stats["total_api_duration_seconds"] += api_seconds
        proj_stats["total_wall_duration_seconds"] += wall_seconds
        proj_stats["total_lines_added"] += lines_added
        proj_stats["total_lines_removed"] += lines_removed
        proj_stats["total_input_tokens"] += input_tokens
        proj_stats["total_output_tokens"] += output_tokens
        proj_stats["total_cache_creation_tokens"] += cache_creation_tokens
        proj_stats["total_cache_read_tokens"] += cache_read_tokens

        # Update per-date stats
        if today not in stats["by_date"]:
            stats["by_date"][today] = self._empty_bucket()
        date_stats = stats["by_date"][today]
        # Initialize token fields if they don't exist (backward compat)
        date_stats.setdefault("total_input_tokens", 0)
        date_stats.setdefault("total_output_tokens", 0)
        date_stats.setdefault("total_cache_creation_tokens", 0)
        date_stats.setdefault("total_cache_read_tokens", 0)
        date_stats["total_cost_cents"] += cost_cents
        date_stats["total_tickets"] += 1
        date_stats["total_api_duration_seconds"] += api_seconds
        date_stats["total_wall_duration_seconds"] += wall_seconds
        date_stats["total_lines_added"] += lines_added
        date_stats["total_lines_removed"] += lines_removed
        date_stats["total_input_tokens"] += input_tokens
        date_stats["total_output_tokens"] += output_tokens
        date_stats["total_cache_creation_tokens"] += cache_creation_tokens
        date_stats["total_cache_read_tokens"] += cache_read_tokens

        # Add to ticket history
        ticket_record = {
            "card_id": card_id,
            "project": project,
            "cost_cents": cost_cents,
            "api_duration_seconds": api_seconds,
            "wall_duration_seconds": wall_seconds,
            "lines_added": lines_added,
            "lines_removed": lines_removed,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_tokens": cache_creation_tokens,
            "cache_read_tokens": cache_read_tokens,
            "processed_at": now,
        }
        stats["ticket_history"].append(ticket_record)

        # Keep only last 100 tickets in history to prevent unbounded growth
        if len(stats["ticket_history"]) > 100:
            stats["ticket_history"] = stats["ticket_history"][-100:]

        logger.info(
            "Recorded stats for card %s: cost=%s, api=%s, wall=%s",
            card_id,
            total_cost or "N/A",
            api_duration or "N/A",
            wall_duration or "N/A",
        )
        self._save()

    def get_stats(self, project: Optional[str] = None) -> AggregatedStats:
        """Get aggregated statistics.

        Args:
            project: Optional project name to filter stats. If None, returns global stats.

        Returns:
            AggregatedStats with the requested statistics.
        """
        if "stats" not in self.state:
            return AggregatedStats()

        stats = self.state["stats"]

        if project:
            # Return project-specific stats
            proj_stats = stats.get("by_project", {}).get(project, {})
            return AggregatedStats(
                total_cost_cents=proj_stats.get("total_cost_cents", 0),
                total_tickets=proj_stats.get("total_tickets", 0),
                total_api_duration_seconds=proj_stats.get("total_api_duration_seconds", 0),
                total_wall_duration_seconds=proj_stats.get("total_wall_duration_seconds", 0),
                total_lines_added=proj_stats.get("total_lines_added", 0),
                total_lines_removed=proj_stats.get("total_lines_removed", 0),
                total_input_tokens=proj_stats.get("total_input_tokens", 0),
                total_output_tokens=proj_stats.get("total_output_tokens", 0),
                total_cache_creation_tokens=proj_stats.get("total_cache_creation_tokens", 0),
                total_cache_read_tokens=proj_stats.get("total_cache_read_tokens", 0),
            )
        else:
            # Return global stats
            global_stats = stats.get("global", {})
            return AggregatedStats(
                total_cost_cents=global_stats.get("total_cost_cents", 0),
                total_tickets=global_stats.get("total_tickets", 0),
                total_api_duration_seconds=global_stats.get("total_api_duration_seconds", 0),
                total_wall_duration_seconds=global_stats.get("total_wall_duration_seconds", 0),
                total_lines_added=global_stats.get("total_lines_added", 0),
                total_lines_removed=global_stats.get("total_lines_removed", 0),
                total_input_tokens=global_stats.get("total_input_tokens", 0),
                total_output_tokens=global_stats.get("total_output_tokens", 0),
                total_cache_creation_tokens=global_stats.get("total_cache_creation_tokens", 0),
                total_cache_read_tokens=global_stats.get("total_cache_read_tokens", 0),
            )

    def get_stats_for_period(self, days: int = 30) -> AggregatedStats:
        """Get statistics for the last N days.

        Args:
            days: Number of days to include (default: 30)

        Returns:
            AggregatedStats for the specified period.
        """
        if "stats" not in self.state:
            return AggregatedStats()

        result = AggregatedStats()
        today = datetime.now(timezone.utc)

        for i in range(days):
            date = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            date_stats = self.state["stats"].get("by_date", {}).get(date, {})
            result.total_cost_cents += date_stats.get("total_cost_cents", 0)
            result.total_tickets += date_stats.get("total_tickets", 0)
            result.total_api_duration_seconds += date_stats.get("total_api_duration_seconds", 0)
            result.total_wall_duration_seconds += date_stats.get("total_wall_duration_seconds", 0)
            result.total_lines_added += date_stats.get("total_lines_added", 0)
            result.total_lines_removed += date_stats.get("total_lines_removed", 0)
            result.total_input_tokens += date_stats.get("total_input_tokens", 0)
            result.total_output_tokens += date_stats.get("total_output_tokens", 0)
            result.total_cache_creation_tokens += date_stats.get("total_cache_creation_tokens", 0)
            result.total_cache_read_tokens += date_stats.get("total_cache_read_tokens", 0)

        return result

    def format_stats_report(self, project: Optional[str] = None) -> str:
        """Format statistics as a human-readable report for Trello comment.

        Args:
            project: Optional project to show stats for. If None, shows all.

        Returns:
            Formatted stats report string.
        """
        lines = ["## TreLLM Usage Statistics\n"]

        # Global stats
        global_stats = self.get_stats()
        lines.append("### All-Time Global Stats")
        lines.append(f"- **Total Cost:** {global_stats.total_cost_dollars}")
        lines.append(f"- **Total Tickets:** {global_stats.total_tickets}")
        lines.append(f"- **Average Cost/Ticket:** {global_stats.average_cost_dollars}")
        lines.append(f"- **API Duration:** {global_stats.api_duration_formatted}")
        lines.append(f"- **Wall Duration:** {global_stats.wall_duration_formatted}")
        lines.append(f"- **Lines Added:** +{global_stats.total_lines_added}")
        lines.append(f"- **Lines Removed:** -{global_stats.total_lines_removed}")
        lines.append("")

        # Token usage section (Claude usage statistics)
        lines.append("### Claude Token Usage (All-Time)")
        lines.append(f"- **Total Tokens:** {global_stats.total_tokens_formatted}")
        lines.append(f"- **Input Tokens:** {global_stats.input_tokens_formatted}")
        lines.append(f"- **Output Tokens:** {global_stats.output_tokens_formatted}")
        lines.append(f"- **Cache Read:** {global_stats.cache_read_tokens_formatted}")
        lines.append("")

        # Last 30 days
        last_30 = self.get_stats_for_period(30)
        lines.append("### Last 30 Days")
        lines.append(f"- **Cost:** {last_30.total_cost_dollars}")
        lines.append(f"- **Tickets:** {last_30.total_tickets}")
        if last_30.total_tickets > 0:
            lines.append(f"- **Avg Cost/Ticket:** {last_30.average_cost_dollars}")
        lines.append(f"- **Tokens:** {last_30.total_tokens_formatted} (in: {last_30.input_tokens_formatted}, out: {last_30.output_tokens_formatted})")
        lines.append("")

        # Per-project stats
        if "stats" in self.state and self.state["stats"].get("by_project"):
            lines.append("### Per-Project Stats")
            for proj_name, proj_data in sorted(self.state["stats"]["by_project"].items()):
                proj_stats = self.get_stats(proj_name)
                lines.append(f"\n**{proj_name}:**")
                lines.append(f"- Cost: {proj_stats.total_cost_dollars}")
                lines.append(f"- Tickets: {proj_stats.total_tickets}")
                if proj_stats.total_tickets > 0:
                    lines.append(f"- Avg: {proj_stats.average_cost_dollars}/ticket")
                lines.append(f"- Changes: +{proj_stats.total_lines_added} -{proj_stats.total_lines_removed}")
                lines.append(f"- Tokens: {proj_stats.total_tokens_formatted} (in: {proj_stats.input_tokens_formatted}, out: {proj_stats.output_tokens_formatted})")

        return "\n".join(lines)
