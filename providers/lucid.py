"""
providers/lucid.py
Lucid (Lucidchart / Lucidspark) SaaS integration plugin.
Implements Lucid OAuth 2.0 and a minimal toolset over the Lucid REST API.
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

logger = logging.getLogger("mcp-providers-lucid")

LUCID_AUTHORIZE_URL = "https://lucid.app/oauth2/authorize"
LUCID_TOKEN_URL = "https://api.lucid.co/oauth2/token"
LUCID_API_BASE = "https://api.lucid.co"

# Single source of truth for Lucid scope strings. The consent URL requests the union
# of these; tool decorators must only reference values from this list.
# Excludes admin-only scopes (account.info, account.user*, account.user.transfercontent).
# Uses parent scopes only — Lucid grants child permissions automatically when a parent
# is granted (e.g. `lucidchart.document.content` implies `:readonly`, `.share.*`, etc.).
SCOPE_USER_PROFILE = "user.profile"
SCOPE_OFFLINE_ACCESS = "offline_access"  # required to receive a refresh_token
SCOPE_FOLDER = "folder"
SCOPE_LUCIDCHART_CONTENT = "lucidchart.document.content"
SCOPE_LUCIDCHART_APP = "lucidchart.document.app"
SCOPE_LUCIDSPARK_CONTENT = "lucidspark.document.content"
SCOPE_LUCIDSPARK_APP = "lucidspark.document.app"
SCOPE_LUCIDSCALE_CONTENT = "lucidscale.document.content"
SCOPE_LUCIDSCALE_APP = "lucidscale.document.app"

ALL_SCOPES = [
    SCOPE_USER_PROFILE,
    SCOPE_OFFLINE_ACCESS,
    SCOPE_FOLDER,
    SCOPE_LUCIDCHART_CONTENT,
    SCOPE_LUCIDCHART_APP,
    SCOPE_LUCIDSPARK_CONTENT,
    SCOPE_LUCIDSPARK_APP,
    SCOPE_LUCIDSCALE_CONTENT,
    SCOPE_LUCIDSCALE_APP,
]
DEFAULT_LUCID_SCOPES = " ".join(ALL_SCOPES)

LUCID_API_VERSION_HEADER = {"Lucid-Api-Version": "1"}


def _parse_scopes(raw) -> list:
    if isinstance(raw, list):
        return [str(s).strip() for s in raw if str(s).strip()]
    return [s.strip() for s in str(raw or "").replace(",", " ").split() if s.strip()]


class LucidProvider(BaseProvider):
    @property
    def name(self) -> str:
        return "lucid"

    def get_auth_url(self, user_email: str) -> str:
        scope = quote(DEFAULT_LUCID_SCOPES)
        return (
            f"{LUCID_AUTHORIZE_URL}"
            f"?client_id={config.LUCID_CLIENT_ID}"
            f"&redirect_uri={config.LUCID_REDIRECT_URI}"
            f"&scope={scope}"
            f"&state={user_email}"
        )

    async def exchange_code_for_tokens(self, code: str, redirect_uri: str) -> dict:
        payload = {
            "code": code,
            "client_id": config.LUCID_CLIENT_ID,
            "client_secret": config.LUCID_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(LUCID_TOKEN_URL, json=payload)
            if response.status_code != 200:
                logger.error(f"Lucid OAuth exchange error: {response.text}")
                raise ValueError(
                    f"Lucid token exchange rejected with status {response.status_code}"
                )
            data = response.json()
            return {
                "access_token": data.get("access_token"),
                "refresh_token": data.get("refresh_token", ""),
                "scopes": _parse_scopes(
                    data.get("scopes") or data.get("scope") or DEFAULT_LUCID_SCOPES
                ),
                "expires_at": compute_expires_at(data.get("expires_in")),
            }

    async def refresh_access_token(self, refresh_token: str) -> dict:
        payload = {
            "refresh_token": refresh_token,
            "client_id": config.LUCID_CLIENT_ID,
            "client_secret": config.LUCID_CLIENT_SECRET,
            "grant_type": "refresh_token",
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(LUCID_TOKEN_URL, json=payload)
            if response.status_code != 200:
                logger.error(f"Lucid refresh error: {response.text}")
                raise ValueError(f"Lucid refresh rejected with status {response.status_code}")
            data = response.json()
            return {
                "access_token": data.get("access_token"),
                "refresh_token": data.get("refresh_token", refresh_token),
                "scopes": _parse_scopes(
                    data.get("scopes") or data.get("scope") or DEFAULT_LUCID_SCOPES
                ),
                "expires_at": compute_expires_at(data.get("expires_in")),
            }

    def register_mcp_tools(self, mcp_app: FastMCP):
        provider = self

        @mcp_app.tool()
        @auth_boundary("lucid")
        @require_scopes([SCOPE_USER_PROFILE])
        async def get_lucid_user_profile() -> str:
            """
            Retrieves the linked Lucid user's profile (name, email, account).
            Trigger this when verifying the linked account or when the user asks "who am I in Lucid?".
            """
            url = f"{LUCID_API_BASE}/users/me"

            async def do_request(token: str) -> httpx.Response:
                async with httpx.AsyncClient() as client:
                    return await client.get(
                        url,
                        headers={
                            "Authorization": f"Bearer {token}",
                            **LUCID_API_VERSION_HEADER,
                            "Accept": "application/json",
                        },
                    )

            try:
                response = await call_with_refresh(provider, do_request)
            except httpx.RequestError as e:
                return f"Network error occurred while connecting to Lucid: {str(e)}"

            if response.status_code == 403:
                return "Error: Access forbidden. The linked account lacks the user.profile scope."
            if response.status_code != 200:
                return f"Lucid API rejected the request. Status code: {response.status_code}."

            data = response.json() or {}
            return (
                f"Lucid User Profile:\n"
                f"- Name: {data.get('fullName', '(unknown)')}\n"
                f"- Email: {data.get('email', '(unknown)')}\n"
                f"- Account ID: {data.get('accountId', '(unknown)')}"
            )

        @mcp_app.tool()
        @auth_boundary("lucid")
        @require_scopes([SCOPE_LUCIDCHART_CONTENT])
        async def get_lucid_document_contents(document_id: str) -> str:
            """
            Retrieves the parsed contents of a Lucidchart or Lucidspark document.
            Trigger this when the user asks to summarize, analyze, or read the shapes/text on a Lucid doc.

            Args:
                document_id (str): The unique document ID.
            """
            url = f"{LUCID_API_BASE}/documents/{document_id}/contents"

            async def do_request(token: str) -> httpx.Response:
                async with httpx.AsyncClient() as client:
                    return await client.get(
                        url,
                        headers={
                            "Authorization": f"Bearer {token}",
                            **LUCID_API_VERSION_HEADER,
                            "Accept": "application/vnd.lucid.contents+json",
                        },
                    )

            try:
                response = await call_with_refresh(provider, do_request)
            except httpx.RequestError as e:
                return f"Network error occurred while connecting to Lucid: {str(e)}"

            if response.status_code == 404:
                return "Error: Lucid document not found. Verify the document_id and access permissions."
            if response.status_code == 403:
                return "Error: Access forbidden. Linked account cannot read this document."
            if response.status_code != 200:
                return f"Lucid API rejected the request. Status code: {response.status_code}."

            data = response.json() or {}
            title = data.get("title", "(untitled)")
            pages = data.get("pages", []) or []
            lines = [f"Document: {title}", f"Pages: {len(pages)}"]
            text_snippets = []
            for page in pages[:10]:
                page_title = page.get("title", "(unnamed page)")
                items = page.get("items", []) or []
                for item in items[:25]:
                    text = (item.get("text") or "").strip()
                    if text:
                        text_snippets.append(f"  [{page_title}] {text}")
            if text_snippets:
                lines.append("Text content (truncated):")
                lines.extend(text_snippets[:50])
            else:
                lines.append("No text content found in the first pages.")
            return "\n".join(lines)

        @mcp_app.tool()
        @auth_boundary("lucid")
        @require_scopes([SCOPE_LUCIDCHART_CONTENT])
        async def search_lucid_documents(keyword: str) -> str:
            """
            Searches the user's accessible Lucid documents by keyword in title.
            Trigger this when the user asks to find a Lucid document by name.

            Args:
                keyword (str): A title fragment to search for.
            """
            url = f"{LUCID_API_BASE}/documents/search"
            body = {"keywords": keyword, "product": ["lucidchart", "lucidspark"]}

            async def do_request(token: str) -> httpx.Response:
                async with httpx.AsyncClient() as client:
                    return await client.post(
                        url,
                        headers={
                            "Authorization": f"Bearer {token}",
                            **LUCID_API_VERSION_HEADER,
                            "Accept": "application/json",
                            "Content-Type": "application/json",
                        },
                        json=body,
                    )

            try:
                response = await call_with_refresh(provider, do_request)
            except httpx.RequestError as e:
                return f"Network error occurred while connecting to Lucid: {str(e)}"

            if response.status_code == 403:
                return "Error: Access forbidden. Linked account lacks document search permission."
            if response.status_code != 200:
                return f"Lucid API rejected the search. Status code: {response.status_code}."

            items = response.json() or []
            if not items:
                return f"No Lucid documents matched '{keyword}'."
            lines = [
                f"- {d.get('title', '(untitled)')} (id: {d.get('documentId', '?')}, product: {d.get('product', '?')})"
                for d in items[:25]
            ]
            return f"Lucid documents matching '{keyword}' ({len(items)} shown):\n" + "\n".join(
                lines
            )
