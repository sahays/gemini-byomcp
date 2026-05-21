"""
providers/figma.py
Figma SaaS integration plugin.
Implements Figma OAuth 2.0 (Basic-auth token endpoint) and a minimal toolset over the
Figma REST API v1.
"""

import base64
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

logger = logging.getLogger("mcp-providers-figma")

FIGMA_AUTHORIZE_URL = "https://www.figma.com/oauth"
FIGMA_TOKEN_URL = "https://api.figma.com/v1/oauth/token"
FIGMA_REFRESH_URL = "https://api.figma.com/v1/oauth/refresh"
FIGMA_API_BASE = "https://api.figma.com/v1"

# Single source of truth for Figma scope strings. The consent URL requests the union
# of these; tool decorators must only reference values from this list.
# Excludes admin-only scopes (org:activity_log_read, org:developer_log_read,
# org:discovery_read) and the deprecated files:read.
SCOPE_CURRENT_USER_READ = "current_user:read"
SCOPE_FILE_CONTENT_READ = "file_content:read"
SCOPE_FILE_METADATA_READ = "file_metadata:read"
SCOPE_FILE_COMMENTS_READ = "file_comments:read"
SCOPE_FILE_COMMENTS_WRITE = "file_comments:write"
SCOPE_FILE_DEV_RESOURCES_READ = "file_dev_resources:read"
SCOPE_FILE_DEV_RESOURCES_WRITE = "file_dev_resources:write"
SCOPE_FILE_VARIABLES_READ = "file_variables:read"  # Enterprise plan only
SCOPE_FILE_VARIABLES_WRITE = "file_variables:write"  # Enterprise plan only
SCOPE_FILE_VERSIONS_READ = "file_versions:read"
SCOPE_LIBRARY_CONTENT_READ = "library_content:read"
SCOPE_LIBRARY_ASSETS_READ = "library_assets:read"
SCOPE_LIBRARY_ANALYTICS_READ = "library_analytics:read"  # Enterprise plan only
SCOPE_TEAM_LIBRARY_CONTENT_READ = "team_library_content:read"
SCOPE_SELECTIONS_READ = "selections:read"
SCOPE_PROJECTS_READ = "projects:read"  # Private OAuth apps only
SCOPE_PROJECT_METADATA_READ = "project_metadata:read"
SCOPE_WEBHOOKS_READ = "webhooks:read"
SCOPE_WEBHOOKS_WRITE = "webhooks:write"

ALL_SCOPES = [
    SCOPE_CURRENT_USER_READ,
    SCOPE_FILE_CONTENT_READ,
    SCOPE_FILE_METADATA_READ,
    SCOPE_FILE_COMMENTS_READ,
    SCOPE_FILE_COMMENTS_WRITE,
    SCOPE_FILE_DEV_RESOURCES_READ,
    SCOPE_FILE_DEV_RESOURCES_WRITE,
    SCOPE_FILE_VARIABLES_READ,
    SCOPE_FILE_VARIABLES_WRITE,
    SCOPE_FILE_VERSIONS_READ,
    SCOPE_LIBRARY_CONTENT_READ,
    SCOPE_LIBRARY_ASSETS_READ,
    SCOPE_LIBRARY_ANALYTICS_READ,
    SCOPE_TEAM_LIBRARY_CONTENT_READ,
    SCOPE_SELECTIONS_READ,
    SCOPE_PROJECTS_READ,
    SCOPE_PROJECT_METADATA_READ,
    SCOPE_WEBHOOKS_READ,
    SCOPE_WEBHOOKS_WRITE,
]
DEFAULT_FIGMA_SCOPES = " ".join(ALL_SCOPES)


def _basic_auth_header() -> str:
    raw = f"{config.FIGMA_CLIENT_ID}:{config.FIGMA_CLIENT_SECRET}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _parse_scopes(raw) -> list:
    if isinstance(raw, list):
        return [str(s).strip() for s in raw if str(s).strip()]
    return [s.strip() for s in str(raw or "").replace(",", " ").split() if s.strip()]


class FigmaProvider(BaseProvider):
    @property
    def name(self) -> str:
        return "figma"

    def get_auth_url(self, user_email: str) -> str:
        scope = quote(DEFAULT_FIGMA_SCOPES)
        return (
            f"{FIGMA_AUTHORIZE_URL}"
            f"?client_id={config.FIGMA_CLIENT_ID}"
            f"&redirect_uri={config.FIGMA_REDIRECT_URI}"
            f"&scope={scope}"
            f"&state={user_email}"
            f"&response_type=code"
        )

    async def exchange_code_for_tokens(self, code: str, redirect_uri: str) -> dict:
        headers = {"Authorization": _basic_auth_header()}
        data = {
            "redirect_uri": redirect_uri,
            "code": code,
            "grant_type": "authorization_code",
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(FIGMA_TOKEN_URL, headers=headers, data=data)
            if response.status_code != 200:
                logger.error(f"Figma OAuth exchange error: {response.text}")
                raise ValueError(
                    f"Figma token exchange rejected with status {response.status_code}"
                )
            payload = response.json()
            return {
                "access_token": payload.get("access_token"),
                "refresh_token": payload.get("refresh_token", ""),
                # Figma doesn't return granted scopes in the token response; persist what we requested.
                "scopes": _parse_scopes(payload.get("scope") or DEFAULT_FIGMA_SCOPES),
                "expires_at": compute_expires_at(payload.get("expires_in")),
            }

    async def refresh_access_token(self, refresh_token: str) -> dict:
        headers = {"Authorization": _basic_auth_header()}
        data = {"refresh_token": refresh_token}
        async with httpx.AsyncClient() as client:
            response = await client.post(FIGMA_REFRESH_URL, headers=headers, data=data)
            if response.status_code != 200:
                logger.error(f"Figma refresh error: {response.text}")
                raise ValueError(f"Figma refresh rejected with status {response.status_code}")
            payload = response.json()
            return {
                "access_token": payload.get("access_token"),
                # Figma's refresh endpoint typically does not return a new refresh_token.
                "refresh_token": payload.get("refresh_token", refresh_token),
                "scopes": _parse_scopes(payload.get("scope") or DEFAULT_FIGMA_SCOPES),
                "expires_at": compute_expires_at(payload.get("expires_in")),
            }

    def register_mcp_tools(self, mcp_app: FastMCP):
        provider = self

        @mcp_app.tool()
        @auth_boundary("figma")
        @require_scopes([SCOPE_FILE_CONTENT_READ])
        async def get_figma_file(file_key: str) -> str:
            """
            Retrieves metadata and the top-level document tree of a Figma file.
            Trigger this when the user asks about the structure, pages, or top-level frames
            of a Figma design file.

            Args:
                file_key (str): The Figma file key (the alphanumeric segment in the file URL).
            """
            url = f"{FIGMA_API_BASE}/files/{file_key}?depth=1"

            async def do_request(token: str) -> httpx.Response:
                async with httpx.AsyncClient() as client:
                    return await client.get(url, headers={"Authorization": f"Bearer {token}"})

            try:
                response = await call_with_refresh(provider, do_request)
            except httpx.RequestError as e:
                return f"Network error occurred while connecting to Figma: {str(e)}"

            if response.status_code == 404:
                return "Error: Figma file not found. Verify the file_key and that your linked account has access."
            if response.status_code == 403:
                return "Error: Access forbidden. The linked account does not have permission to view this file."
            if response.status_code != 200:
                return f"Figma API rejected the request. Status code: {response.status_code}."

            data = response.json()
            name = data.get("name", "(untitled)")
            last_modified = data.get("lastModified", "?")
            doc = data.get("document", {}) or {}
            pages = [child.get("name", "(unnamed)") for child in doc.get("children", [])]
            summary = [
                f"File: {name}",
                f"Last modified: {last_modified}",
                f"Pages ({len(pages)}): " + (", ".join(pages) if pages else "(none)"),
            ]
            return "\n".join(summary)

        @mcp_app.tool()
        @auth_boundary("figma")
        @require_scopes([SCOPE_FILE_CONTENT_READ])
        async def get_figma_file_nodes(file_key: str, node_ids: str) -> str:
            """
            Retrieves specific nodes from a Figma file by ID. Returns a concise summary including
            node type and name. Use this when the user references specific frames, components,
            or layers by their node IDs.

            Args:
                file_key (str): The Figma file key.
                node_ids (str): Comma-separated node IDs (e.g. "1:2,3:4").
            """
            ids_param = quote(node_ids)
            url = f"{FIGMA_API_BASE}/files/{file_key}/nodes?ids={ids_param}"

            async def do_request(token: str) -> httpx.Response:
                async with httpx.AsyncClient() as client:
                    return await client.get(url, headers={"Authorization": f"Bearer {token}"})

            try:
                response = await call_with_refresh(provider, do_request)
            except httpx.RequestError as e:
                return f"Network error occurred while connecting to Figma: {str(e)}"

            if response.status_code == 404:
                return "Error: Figma file or nodes not found."
            if response.status_code == 403:
                return "Error: Access forbidden for the requested file/nodes."
            if response.status_code != 200:
                return f"Figma API rejected the request. Status code: {response.status_code}."

            nodes = (response.json() or {}).get("nodes", {})
            if not nodes:
                return "No matching nodes were returned."

            lines = []
            for node_id, wrapper in nodes.items():
                if not wrapper:
                    lines.append(f"- {node_id}: (not found)")
                    continue
                doc = wrapper.get("document", {}) or {}
                lines.append(f'- {node_id}: {doc.get("type", "?")} "{doc.get("name", "")}"')
            return "Figma Nodes:\n" + "\n".join(lines)

        @mcp_app.tool()
        @auth_boundary("figma")
        @require_scopes([SCOPE_LIBRARY_CONTENT_READ])
        async def get_figma_team_components(team_id: str) -> str:
            """
            Lists published components for a Figma team's libraries.
            Trigger this when the user asks about design-system components shared by a team.

            Args:
                team_id (str): The Figma team ID.
            """
            url = f"{FIGMA_API_BASE}/teams/{team_id}/components?page_size=30"

            async def do_request(token: str) -> httpx.Response:
                async with httpx.AsyncClient() as client:
                    return await client.get(url, headers={"Authorization": f"Bearer {token}"})

            try:
                response = await call_with_refresh(provider, do_request)
            except httpx.RequestError as e:
                return f"Network error occurred while connecting to Figma: {str(e)}"

            if response.status_code == 404:
                return "Error: Team not found or has no published components."
            if response.status_code == 403:
                return "Error: Access forbidden. The linked account lacks permission for this team."
            if response.status_code != 200:
                return f"Figma API rejected the request. Status code: {response.status_code}."

            meta = ((response.json() or {}).get("meta") or {}).get("components", [])
            if not meta:
                return "No published components were found for this team."
            lines = [
                f"- {c.get('name', '(unnamed)')} (key: {c.get('key', '?')})" for c in meta[:30]
            ]
            return f"Figma Team Components ({len(meta)} shown):\n" + "\n".join(lines)
