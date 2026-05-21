"""
providers/miro.py
Miro SaaS integration plugin.
Handles Miro-specific OAuth scopes (boards:read, boards:write) and REST API v2 integrations.
"""

import logging
from urllib.parse import quote

import httpx
from fastmcp import FastMCP

from config import config
from providers.base import (
    BaseProvider,
    auth_boundary,
    call_with_refresh,
    compute_expires_at,
    require_scopes,
)

logger = logging.getLogger("mcp-providers-miro")

MIRO_AUTHORIZE_URL = "https://miro.com/oauth/authorize"
MIRO_TOKEN_URL = "https://api.miro.com/v1/oauth/token"
MIRO_API_BASE = "https://api.miro.com/v2"

# Single source of truth for Miro scope strings. The consent URL requests the union
# of these; tool decorators must only reference values from this list.
# Excludes admin-only scopes (organizations:*, auditlogs:read, contentlogs:export,
# organizations:cases:management, sessions:delete).
SCOPE_BOARDS_READ = "boards:read"
SCOPE_BOARDS_WRITE = "boards:write"
SCOPE_BOARDS_EXPORT = "boards:export"
SCOPE_IDENTITY_READ = "identity:read"
SCOPE_PROJECTS_READ = "projects:read"
SCOPE_PROJECTS_WRITE = "projects:write"

ALL_SCOPES = [
    SCOPE_BOARDS_READ,
    SCOPE_BOARDS_WRITE,
    SCOPE_BOARDS_EXPORT,
    SCOPE_IDENTITY_READ,
    SCOPE_PROJECTS_READ,
    SCOPE_PROJECTS_WRITE,
]


def _parse_scopes(raw: str) -> list:
    return [s.strip() for s in (raw or "").replace(",", " ").split() if s.strip()]


class MiroProvider(BaseProvider):
    @property
    def name(self) -> str:
        return "miro"

    def get_auth_url(self, user_email: str) -> str:
        scope = quote(" ".join(ALL_SCOPES))
        return (
            f"{MIRO_AUTHORIZE_URL}"
            f"?response_type=code"
            f"&client_id={config.MIRO_CLIENT_ID}"
            f"&redirect_uri={config.MIRO_REDIRECT_URI}"
            f"&scope={scope}"
            f"&state={user_email}"
        )

    async def exchange_code_for_tokens(self, code: str, redirect_uri: str) -> dict:
        payload = {
            "grant_type": "authorization_code",
            "client_id": config.MIRO_CLIENT_ID,
            "client_secret": config.MIRO_CLIENT_SECRET,
            "code": code,
            "redirect_uri": redirect_uri,
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(MIRO_TOKEN_URL, params=payload)
            if response.status_code != 200:
                logger.error(f"Miro OAuth exchange error: {response.text}")
                raise ValueError(f"Miro token exchange rejected with status {response.status_code}")

            data = response.json()
            return {
                "access_token": data.get("access_token"),
                "refresh_token": data.get("refresh_token", ""),
                "scopes": _parse_scopes(data.get("scope", "")),
                "expires_at": compute_expires_at(data.get("expires_in")),
            }

    async def refresh_access_token(self, refresh_token: str) -> dict:
        payload = {
            "grant_type": "refresh_token",
            "client_id": config.MIRO_CLIENT_ID,
            "client_secret": config.MIRO_CLIENT_SECRET,
            "refresh_token": refresh_token,
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(MIRO_TOKEN_URL, params=payload)
            if response.status_code != 200:
                logger.error(f"Miro refresh error: {response.text}")
                raise ValueError(f"Miro refresh rejected with status {response.status_code}")

            data = response.json()
            return {
                "access_token": data.get("access_token"),
                "refresh_token": data.get("refresh_token", refresh_token),
                "scopes": _parse_scopes(data.get("scope", "")),
                "expires_at": compute_expires_at(data.get("expires_in")),
            }

    def register_mcp_tools(self, mcp_app: FastMCP):
        provider = self

        @mcp_app.tool()
        @auth_boundary("miro")
        @require_scopes([SCOPE_BOARDS_READ])
        async def get_miro_board_items(board_id: str) -> str:
            """
            Retrieves and parses text-based items (sticky notes, text boxes, and shapes) from a specific Miro board.
            Trigger this when you need to read board content for summaries, diagram analysis, or meeting extractions.

            Args:
                board_id (str): The unique alphanumeric ID of the Miro board.
            """
            url = f"{MIRO_API_BASE}/boards/{board_id}/items?limit=50"

            async def do_request(token: str) -> httpx.Response:
                async with httpx.AsyncClient() as client:
                    return await client.get(
                        url,
                        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    )

            try:
                response = await call_with_refresh(provider, do_request)
            except httpx.RequestError as e:
                return f"Network error occurred while connecting to Miro: {str(e)}"

            if response.status_code == 404:
                return "Error: Board not found. Verify the board_id is correct and your linked account has access."
            if response.status_code == 403:
                return "Error: Access forbidden. You may not have permission to view this specific board."
            if response.status_code != 200:
                return f"Miro API rejected the request. Status code: {response.status_code}."

            data = response.json()
            items = data.get("data", [])
            if not items:
                return "The board is empty or no readable items were found."

            board_content = []
            for item in items:
                content = item.get("data", {}).get("content", "")
                if content:
                    clean_content = content.replace("<p>", "").replace("</p>", "\n").strip()
                    if clean_content:
                        board_content.append(f"- {item['type'].capitalize()}: {clean_content}")

            if not board_content:
                return "No text-based items found on the board."
            return "Miro Board Content:\n" + "\n".join(board_content)

        @mcp_app.tool()
        @auth_boundary("miro")
        @require_scopes([SCOPE_BOARDS_WRITE])
        async def create_miro_sticky_note(board_id: str, content: str) -> str:
            """
            Creates a new sticky note with the specified text content on a Miro board.
            Trigger this when the user requests to write, add notes, or brainstorm on a board.

            Args:
                board_id (str): The unique alphanumeric ID of the Miro board.
                content (str): The text content to display inside the sticky note.
            """
            url = f"{MIRO_API_BASE}/boards/{board_id}/items"
            payload = {
                "data": {"content": content, "shape": "square"},
                "style": {"fillColor": "light_yellow"},
                "type": "sticky_note",
            }

            async def do_request(token: str) -> httpx.Response:
                async with httpx.AsyncClient() as client:
                    return await client.post(
                        url,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Content-Type": "application/json",
                            "Accept": "application/json",
                        },
                        json=payload,
                    )

            try:
                response = await call_with_refresh(provider, do_request)
            except httpx.RequestError as e:
                return f"Network error occurred while connecting to Miro: {str(e)}"

            if response.status_code == 404:
                return "Error: Board not found. Verify the board ID."
            if response.status_code == 403:
                return "Error: Access Forbidden. The token lacks boards:write or you do not have permission."
            if response.status_code not in (200, 201):
                return (
                    f"Miro API rejected sticky note creation. Status code: {response.status_code}."
                )

            item_data = response.json()
            item_id = item_data.get("id", "Unknown ID")
            return f"Successfully created sticky note (ID: {item_id}) with content: '{content}'"

        @mcp_app.tool()
        @auth_boundary("miro")
        @require_scopes([SCOPE_BOARDS_WRITE])
        async def delete_miro_board_item(board_id: str, item_id: str) -> str:
            """
            Deletes an item (sticky note, shape, text box) from a specific Miro board.
            Trigger this when the user explicitly asks to remove, delete, or clean up an item.

            Args:
                board_id (str): The unique alphanumeric ID of the Miro board.
                item_id (str): The unique alphanumeric ID of the item to delete.
            """
            url = f"{MIRO_API_BASE}/boards/{board_id}/items/{item_id}"

            async def do_request(token: str) -> httpx.Response:
                async with httpx.AsyncClient() as client:
                    return await client.delete(
                        url,
                        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    )

            try:
                response = await call_with_refresh(provider, do_request)
            except httpx.RequestError as e:
                return f"Network error occurred while connecting to Miro: {str(e)}"

            if response.status_code == 404:
                return "Error: Item or Board not found. Verify the IDs."
            if response.status_code == 403:
                return "Error: Access Forbidden. This action requires boards:write scopes."
            if response.status_code not in (200, 204):
                return f"Miro API rejected deletion. Status code: {response.status_code}."

            return f"Successfully deleted item (ID: {item_id}) from board (ID: {board_id})."
