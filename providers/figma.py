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
# Excludes:
#   - admin-only scopes (org:*)
#   - the deprecated files:read
#   - selections:read (no REST endpoint exists; plugin API only)
#   - projects:read / project_metadata:read (Tier-2 / private-OAuth-app only)
#   - webhooks:* (operational, not a user query)
#   - Enterprise-tier scopes (file_dev_resources:write, file_variables:*,
#     library_analytics:read) — not selectable in standard Figma apps.
SCOPE_CURRENT_USER_READ = "current_user:read"
SCOPE_FILE_CONTENT_READ = "file_content:read"
SCOPE_FILE_METADATA_READ = "file_metadata:read"
SCOPE_FILE_COMMENTS_READ = "file_comments:read"
SCOPE_FILE_COMMENTS_WRITE = "file_comments:write"
SCOPE_FILE_DEV_RESOURCES_READ = "file_dev_resources:read"
SCOPE_FILE_VERSIONS_READ = "file_versions:read"
SCOPE_LIBRARY_CONTENT_READ = "library_content:read"
SCOPE_LIBRARY_ASSETS_READ = "library_assets:read"
SCOPE_TEAM_LIBRARY_CONTENT_READ = "team_library_content:read"

ALL_SCOPES = [
    SCOPE_CURRENT_USER_READ,
    SCOPE_FILE_CONTENT_READ,
    SCOPE_FILE_METADATA_READ,
    SCOPE_FILE_COMMENTS_READ,
    SCOPE_FILE_COMMENTS_WRITE,
    SCOPE_FILE_DEV_RESOURCES_READ,
    SCOPE_FILE_VERSIONS_READ,
    SCOPE_LIBRARY_CONTENT_READ,
    SCOPE_LIBRARY_ASSETS_READ,
    SCOPE_TEAM_LIBRARY_CONTENT_READ,
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

        async def _figma_get(token: str, path: str) -> httpx.Response:
            async with httpx.AsyncClient() as client:
                return await client.get(
                    f"{FIGMA_API_BASE}{path}", headers={"Authorization": f"Bearer {token}"}
                )

        async def _figma_post(token: str, path: str, payload: dict) -> httpx.Response:
            async with httpx.AsyncClient() as client:
                return await client.post(
                    f"{FIGMA_API_BASE}{path}",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )

        def _figma_error(response: httpx.Response, what: str) -> str:
            if response.status_code == 403:
                return f"Error: Access forbidden ({what}). The token lacks the required scope."
            if response.status_code == 404:
                return f"Error: Not found ({what})."
            return f"Figma API rejected the request ({what}). Status code: {response.status_code}."

        @mcp_app.tool()
        @auth_boundary("figma")
        @require_scopes([SCOPE_TEAM_LIBRARY_CONTENT_READ])
        async def get_figma_team_components(team_id: str) -> str:
            """
            Lists published components for a Figma team's libraries.
            Trigger this when the user asks about design-system components shared by a team.

            Args:
                team_id (str): The Figma team ID.
            """
            try:
                response = await call_with_refresh(
                    provider, lambda t: _figma_get(t, f"/teams/{team_id}/components?page_size=30")
                )
            except httpx.RequestError as e:
                return f"Network error occurred while connecting to Figma: {str(e)}"
            if response.status_code != 200:
                return _figma_error(response, "team components")
            meta = ((response.json() or {}).get("meta") or {}).get("components", [])
            if not meta:
                return "No published components were found for this team."
            lines = [
                f"- {c.get('name', '(unnamed)')} (key: {c.get('key', '?')})" for c in meta[:30]
            ]
            return f"Figma Team Components ({len(meta)} shown):\n" + "\n".join(lines)

        # ---- current user (zero-arg discovery tool, no textbox in GE) ----

        @mcp_app.tool()
        @auth_boundary("figma")
        @require_scopes([SCOPE_CURRENT_USER_READ])
        async def get_figma_me() -> str:
            """
            Returns the signed-in Figma user's identity (id, name, email, avatar).
            Use this when the user asks "who am I in Figma?" or you need to confirm
            which Figma account is linked. Takes no arguments — call directly.
            """
            try:
                response = await call_with_refresh(provider, lambda t: _figma_get(t, "/me"))
            except httpx.RequestError as e:
                return f"Network error occurred while connecting to Figma: {str(e)}"
            if response.status_code != 200:
                return _figma_error(response, "current user")
            u = response.json() or {}
            return (
                f"Figma user: {u.get('handle', u.get('email', '?'))} "
                f"(id: {u.get('id', '?')}, email: {u.get('email', '?')})"
            )

        # ---- file metadata + images ----

        @mcp_app.tool()
        @auth_boundary("figma")
        @require_scopes([SCOPE_FILE_METADATA_READ])
        async def get_figma_file_metadata(file_key: str) -> str:
            """
            Retrieves lightweight metadata for a Figma file (name, last modified, role,
            editor type) without fetching the full document tree. Use this when the user
            only needs basic info about a file.

            Args:
                file_key (str): The Figma file key (from the file URL).
            """
            try:
                response = await call_with_refresh(
                    provider, lambda t: _figma_get(t, f"/files/{file_key}/meta")
                )
            except httpx.RequestError as e:
                return f"Network error occurred while connecting to Figma: {str(e)}"
            if response.status_code != 200:
                return _figma_error(response, "file metadata")
            f = (response.json() or {}).get("file") or response.json() or {}
            return (
                f"File: {f.get('name', '(untitled)')}\n"
                f"Last modified: {f.get('last_modified', f.get('lastModified', '?'))}\n"
                f"Editor type: {f.get('editor_type', f.get('editorType', '?'))}\n"
                f"Role: {f.get('role', '?')}"
            )

        @mcp_app.tool()
        @auth_boundary("figma")
        @require_scopes([SCOPE_FILE_CONTENT_READ])
        async def render_figma_file_images(
            file_key: str, node_ids: str, format: str = "png"
        ) -> str:
            """
            Renders specific nodes (frames, components) in a Figma file as image URLs.
            Use this when the user asks to "see", "preview", "render", or "export" a
            specific frame from a file.

            Args:
                file_key (str): The Figma file key.
                node_ids (str): Comma-separated node ids to render.
                format (str): One of "png", "jpg", "svg", "pdf". Defaults to "png".
            """
            ids = quote(node_ids)
            try:
                response = await call_with_refresh(
                    provider,
                    lambda t: _figma_get(t, f"/images/{file_key}?ids={ids}&format={format}"),
                )
            except httpx.RequestError as e:
                return f"Network error occurred while connecting to Figma: {str(e)}"
            if response.status_code != 200:
                return _figma_error(response, "image render")
            images = (response.json() or {}).get("images", {}) or {}
            if not images:
                return "No images were rendered for the requested nodes."
            lines = [f"- {nid}: {url or '(failed)'}" for nid, url in images.items()]
            return f"Figma Image Renders ({format}):\n" + "\n".join(lines)

        @mcp_app.tool()
        @auth_boundary("figma")
        @require_scopes([SCOPE_FILE_CONTENT_READ])
        async def get_figma_file_image_fills(file_key: str) -> str:
            """
            Lists the image fills (uploaded raster images) used inside a Figma file with
            download URLs. Use this when the user asks about photos/images embedded in
            a file.

            Args:
                file_key (str): The Figma file key.
            """
            try:
                response = await call_with_refresh(
                    provider, lambda t: _figma_get(t, f"/files/{file_key}/images")
                )
            except httpx.RequestError as e:
                return f"Network error occurred while connecting to Figma: {str(e)}"
            if response.status_code != 200:
                return _figma_error(response, "image fills")
            meta = (response.json() or {}).get("meta") or {}
            images = meta.get("images") or {}
            if not images:
                return "No image fills were found in this file."
            lines = [f"- {iid}: {url}" for iid, url in list(images.items())[:30]]
            return f"Figma Image Fills ({len(images)} shown):\n" + "\n".join(lines)

        # ---- comments ----

        @mcp_app.tool()
        @auth_boundary("figma")
        @require_scopes([SCOPE_FILE_COMMENTS_READ])
        async def get_figma_file_comments(file_key: str) -> str:
            """
            Lists comments on a Figma file. Use this when the user asks about feedback,
            review notes, or discussion on a file.

            Args:
                file_key (str): The Figma file key.
            """
            try:
                response = await call_with_refresh(
                    provider, lambda t: _figma_get(t, f"/files/{file_key}/comments")
                )
            except httpx.RequestError as e:
                return f"Network error occurred while connecting to Figma: {str(e)}"
            if response.status_code != 200:
                return _figma_error(response, "comments")
            comments = (response.json() or {}).get("comments", []) or []
            if not comments:
                return "No comments on this file."
            lines = [
                f"- [{c.get('user', {}).get('handle', '?')}] {c.get('message', '')[:160]} "
                f"(id: {c.get('id', '?')})"
                for c in comments[:30]
            ]
            return f"Figma Comments ({len(comments)} total, showing {len(lines)}):\n" + "\n".join(
                lines
            )

        @mcp_app.tool()
        @auth_boundary("figma")
        @require_scopes([SCOPE_FILE_COMMENTS_WRITE])
        async def post_figma_file_comment(file_key: str, message: str) -> str:
            """
            Posts a top-level comment to a Figma file. Use this when the user explicitly
            asks to leave a comment or feedback on a file.

            Args:
                file_key (str): The Figma file key.
                message (str): The comment text.
            """
            try:
                response = await call_with_refresh(
                    provider,
                    lambda t: _figma_post(t, f"/files/{file_key}/comments", {"message": message}),
                )
            except httpx.RequestError as e:
                return f"Network error occurred while connecting to Figma: {str(e)}"
            if response.status_code not in (200, 201):
                return _figma_error(response, "post comment")
            c = response.json() or {}
            return f"Posted comment (id: {c.get('id', '?')}) on file {file_key}."

        # ---- versions & dev resources ----

        @mcp_app.tool()
        @auth_boundary("figma")
        @require_scopes([SCOPE_FILE_VERSIONS_READ])
        async def get_figma_file_versions(file_key: str) -> str:
            """
            Lists the version history of a Figma file (label, description, created_at, author).
            Use this when the user asks about file history or wants to see past saves.

            Args:
                file_key (str): The Figma file key.
            """
            try:
                response = await call_with_refresh(
                    provider, lambda t: _figma_get(t, f"/files/{file_key}/versions")
                )
            except httpx.RequestError as e:
                return f"Network error occurred while connecting to Figma: {str(e)}"
            if response.status_code != 200:
                return _figma_error(response, "versions")
            versions = (response.json() or {}).get("versions", []) or []
            if not versions:
                return "No saved versions for this file."
            lines = [
                f"- {v.get('created_at', '?')} | "
                f"{v.get('label') or v.get('description') or '(no label)'} "
                f"by {v.get('user', {}).get('handle', '?')}"
                for v in versions[:20]
            ]
            return (
                f"Figma File Versions ({len(versions)} total, showing {len(lines)}):\n"
                + "\n".join(lines)
            )

        @mcp_app.tool()
        @auth_boundary("figma")
        @require_scopes([SCOPE_FILE_DEV_RESOURCES_READ])
        async def get_figma_file_dev_resources(file_key: str) -> str:
            """
            Lists dev resources (engineering links: GitHub PRs, Storybook entries, Jira tickets)
            attached to nodes within a Figma file. Use this when the user asks about the
            engineering hand-off, code links, or dev mode resources on a file.

            Args:
                file_key (str): The Figma file key.
            """
            try:
                response = await call_with_refresh(
                    provider, lambda t: _figma_get(t, f"/files/{file_key}/dev_resources")
                )
            except httpx.RequestError as e:
                return f"Network error occurred while connecting to Figma: {str(e)}"
            if response.status_code != 200:
                return _figma_error(response, "dev resources")
            resources = (response.json() or {}).get("dev_resources", []) or []
            if not resources:
                return "No dev resources are attached to this file."
            lines = [
                f"- {r.get('name', '(unnamed)')}: {r.get('url', '?')} (node: {r.get('node_id', '?')})"
                for r in resources[:30]
            ]
            return f"Figma Dev Resources ({len(resources)} total):\n" + "\n".join(lines)

        # ---- library content (per-file + per-team) ----

        @mcp_app.tool()
        @auth_boundary("figma")
        @require_scopes([SCOPE_LIBRARY_CONTENT_READ])
        async def get_figma_file_components(file_key: str) -> str:
            """
            Lists components published from a Figma file's library. Use this when the user
            asks about reusable components defined in a specific file.

            Args:
                file_key (str): The Figma file key.
            """
            try:
                response = await call_with_refresh(
                    provider, lambda t: _figma_get(t, f"/files/{file_key}/components")
                )
            except httpx.RequestError as e:
                return f"Network error occurred while connecting to Figma: {str(e)}"
            if response.status_code != 200:
                return _figma_error(response, "file components")
            meta = ((response.json() or {}).get("meta") or {}).get("components") or []
            if not meta:
                return "No published components in this file."
            lines = [
                f"- {c.get('name', '(unnamed)')} (key: {c.get('key', '?')})" for c in meta[:30]
            ]
            return f"Figma File Components ({len(meta)} shown):\n" + "\n".join(lines)

        @mcp_app.tool()
        @auth_boundary("figma")
        @require_scopes([SCOPE_LIBRARY_CONTENT_READ])
        async def get_figma_file_styles(file_key: str) -> str:
            """
            Lists styles (color, text, effect, grid) published from a Figma file's library.
            Use this when the user asks about styles or design tokens defined in a file.

            Args:
                file_key (str): The Figma file key.
            """
            try:
                response = await call_with_refresh(
                    provider, lambda t: _figma_get(t, f"/files/{file_key}/styles")
                )
            except httpx.RequestError as e:
                return f"Network error occurred while connecting to Figma: {str(e)}"
            if response.status_code != 200:
                return _figma_error(response, "file styles")
            meta = ((response.json() or {}).get("meta") or {}).get("styles") or []
            if not meta:
                return "No published styles in this file."
            lines = [
                f"- {s.get('name', '(unnamed)')} [{s.get('style_type', '?')}] (key: {s.get('key', '?')})"
                for s in meta[:30]
            ]
            return f"Figma File Styles ({len(meta)} shown):\n" + "\n".join(lines)

        @mcp_app.tool()
        @auth_boundary("figma")
        @require_scopes([SCOPE_TEAM_LIBRARY_CONTENT_READ])
        async def get_figma_team_styles(team_id: str) -> str:
            """
            Lists styles published from a Figma team's libraries. Use this when the user
            asks about design tokens or styles shared by a team.

            Args:
                team_id (str): The Figma team ID.
            """
            try:
                response = await call_with_refresh(
                    provider, lambda t: _figma_get(t, f"/teams/{team_id}/styles?page_size=30")
                )
            except httpx.RequestError as e:
                return f"Network error occurred while connecting to Figma: {str(e)}"
            if response.status_code != 200:
                return _figma_error(response, "team styles")
            meta = ((response.json() or {}).get("meta") or {}).get("styles", []) or []
            if not meta:
                return "No published styles for this team."
            lines = [
                f"- {s.get('name', '(unnamed)')} [{s.get('style_type', '?')}] (key: {s.get('key', '?')})"
                for s in meta[:30]
            ]
            return f"Figma Team Styles ({len(meta)} shown):\n" + "\n".join(lines)

        @mcp_app.tool()
        @auth_boundary("figma")
        @require_scopes([SCOPE_LIBRARY_ASSETS_READ])
        async def get_figma_component(key: str) -> str:
            """
            Retrieves details for a single Figma component by its component key.
            Use this when the user references a specific component by key (e.g. from
            get_figma_team_components output).

            Args:
                key (str): The Figma component key.
            """
            try:
                response = await call_with_refresh(
                    provider, lambda t: _figma_get(t, f"/components/{key}")
                )
            except httpx.RequestError as e:
                return f"Network error occurred while connecting to Figma: {str(e)}"
            if response.status_code != 200:
                return _figma_error(response, "component")
            c = (response.json() or {}).get("meta") or {}
            return (
                f"Figma Component: {c.get('name', '(unnamed)')}\n"
                f"  key: {c.get('key', '?')}\n"
                f"  file_key: {c.get('file_key', '?')}\n"
                f"  description: {c.get('description', '(none)')}\n"
                f"  created_at: {c.get('created_at', '?')}"
            )

        @mcp_app.tool()
        @auth_boundary("figma")
        @require_scopes([SCOPE_LIBRARY_ASSETS_READ])
        async def get_figma_style(key: str) -> str:
            """
            Retrieves details for a single Figma style by its style key.
            Use this when the user references a specific style by key.

            Args:
                key (str): The Figma style key.
            """
            try:
                response = await call_with_refresh(
                    provider, lambda t: _figma_get(t, f"/styles/{key}")
                )
            except httpx.RequestError as e:
                return f"Network error occurred while connecting to Figma: {str(e)}"
            if response.status_code != 200:
                return _figma_error(response, "style")
            s = (response.json() or {}).get("meta") or {}
            return (
                f"Figma Style: {s.get('name', '(unnamed)')} [{s.get('style_type', '?')}]\n"
                f"  key: {s.get('key', '?')}\n"
                f"  file_key: {s.get('file_key', '?')}\n"
                f"  description: {s.get('description', '(none)')}"
            )
