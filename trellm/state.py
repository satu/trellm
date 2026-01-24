"""State persistence for TreLLM."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


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
                return json.loads(self.path.read_text())
            except Exception as e:
                logger.error("Failed to load state from %s: %s", self.path, e)
        return {"sessions": {}, "processed": {}}

    def _save(self) -> None:
        """Save state to file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state, indent=2))

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
