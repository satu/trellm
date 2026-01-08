"""Trello API client for TreLLM."""

import logging
from dataclasses import dataclass
from typing import Optional

import aiohttp

from .config import TrelloConfig

logger = logging.getLogger(__name__)


@dataclass
class TrelloCard:
    """Represents a Trello card."""

    id: str
    name: str
    description: str
    url: str
    last_activity: str


class TrelloClient:
    """Async client for Trello API."""

    BASE_URL = "https://api.trello.com/1"

    def __init__(self, config: TrelloConfig):
        self.api_key = config.api_key
        self.api_token = config.api_token
        self.board_id = config.board_id
        self.todo_list_id = config.todo_list_id
        self.ready_list_id = config.ready_to_try_list_id
        # Optional: destination board/list for completed cards
        self.done_board_id = config.done_board_id
        self.done_list_id = config.done_list_id
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create an aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """Close the aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        json_data: Optional[dict] = None,
    ) -> dict:
        """Make an authenticated request to Trello API."""
        url = f"{self.BASE_URL}{path}"

        # Add auth params
        request_params = params or {}
        request_params["key"] = self.api_key
        request_params["token"] = self.api_token

        session = await self._get_session()
        async with session.request(
            method, url, params=request_params, json=json_data
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_todo_cards(self) -> list[TrelloCard]:
        """Get all cards in the TODO list."""
        data = await self._request("GET", f"/lists/{self.todo_list_id}/cards")
        return [
            TrelloCard(
                id=c["id"],
                name=c["name"],
                description=c.get("desc", ""),
                url=c["url"],
                last_activity=c.get("dateLastActivity", ""),
            )
            for c in data
        ]

    async def move_to_ready(self, card_id: str) -> None:
        """Move a card to the READY TO TRY list.

        If done_board_id and done_list_id are configured, moves the card
        to that board/list. Otherwise, moves to the READY TO TRY list
        on the same board.
        """
        # If a separate done board is configured, use it
        if self.done_board_id and self.done_list_id:
            await self._request(
                "PUT",
                f"/cards/{card_id}",
                json_data={
                    "idList": self.done_list_id,
                    "idBoard": self.done_board_id,
                },
            )
            logger.info(
                "Moved card %s to board %s list %s",
                card_id,
                self.done_board_id,
                self.done_list_id,
            )
            return

        # Fall back to ready_to_try_list_id on the same board
        if not self.ready_list_id:
            # Discover the list by name
            lists = await self._request("GET", f"/boards/{self.board_id}/lists")
            for lst in lists:
                if lst["name"] == "READY TO TRY":
                    self.ready_list_id = lst["id"]
                    break

        if self.ready_list_id:
            await self._request(
                "PUT",
                f"/cards/{card_id}",
                json_data={"idList": self.ready_list_id},
            )
            logger.info("Moved card %s to READY TO TRY", card_id)
        else:
            logger.warning("Could not find READY TO TRY list")
