"""
Lucid provider tests — JSON-body token exchange/refresh and downstream Lucid REST calls
(including the Lucid-Api-Version header and the contents Accept type).
"""

import json

import httpx
import pytest
import respx

from providers.lucid import LucidProvider


def test_get_auth_url_includes_offline_access():
    provider = LucidProvider()
    url = provider.get_auth_url("alice@example.com")
    assert url.startswith("https://lucid.app/oauth2/authorize?")
    assert "client_id=lucid-test-id" in url
    assert "state=alice@example.com" in url
    # Minimum-viable scope set: identity + document content + refresh-token.
    for scope in (
        "user.profile",
        "offline_access",  # required to get a refresh_token from Lucid
        "lucidchart.document.content",
    ):
        assert scope in url, f"Missing {scope} from Lucid consent URL"
    for excluded in (
        "folder",
        "lucidchart.document.app",
        "lucidspark.document.content",
        "lucidspark.document.app",
        "lucidscale.document.content",
        "lucidscale.document.app",
    ):
        assert excluded not in url, f"Unexpected scope {excluded} in consent URL"


@respx.mock
async def test_exchange_code_sends_json_body():
    provider = LucidProvider()
    route = respx.post("https://api.lucid.co/oauth2/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "AT",
                "refresh_token": "RT",
                "expires_in": 3600,
                "scopes": ["user.profile", "lucidchart.document.content"],
                "token_type": "bearer",
            },
        )
    )

    result = await provider.exchange_code_for_tokens(
        "code-x", "http://localhost:8080/auth/lucid/callback"
    )

    assert route.called
    req = route.calls.last.request
    body = json.loads(req.read().decode())
    assert body["grant_type"] == "authorization_code"
    assert body["code"] == "code-x"
    assert body["client_id"] == "lucid-test-id"
    assert body["client_secret"] == "lucid-test-secret"
    assert body["redirect_uri"] == "http://localhost:8080/auth/lucid/callback"

    assert result["access_token"] == "AT"
    assert result["refresh_token"] == "RT"
    assert "user.profile" in result["scopes"]


@respx.mock
async def test_refresh_sends_grant_type_refresh():
    provider = LucidProvider()
    route = respx.post("https://api.lucid.co/oauth2/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "NEW",
                "refresh_token": "NEW-R",
                "expires_in": 3600,
                "scopes": [],
            },
        )
    )

    result = await provider.refresh_access_token("OLD-R")
    body = json.loads(route.calls.last.request.read().decode())
    assert body["grant_type"] == "refresh_token"
    assert body["refresh_token"] == "OLD-R"
    assert body["client_id"] == "lucid-test-id"

    assert result["access_token"] == "NEW"
    assert result["refresh_token"] == "NEW-R"


@respx.mock
async def test_exchange_failure_raises():
    provider = LucidProvider()
    respx.post("https://api.lucid.co/oauth2/token").mock(
        return_value=httpx.Response(401, json={"error": "bad"})
    )
    with pytest.raises(ValueError):
        await provider.exchange_code_for_tokens("c", "http://x")


@respx.mock
async def test_get_lucid_user_profile_sends_api_version_header(set_user_context, temp_db):
    set_user_context(email="alice@example.com", token="AT", scopes=["user.profile"])
    await temp_db.save_tokens("alice@example.com", "lucid", "AT", "RT", ["user.profile"], "")

    route = respx.get("https://api.lucid.co/users/me").mock(
        return_value=httpx.Response(
            200, json={"fullName": "Alice", "email": "alice@example.com", "accountId": "acc-1"}
        )
    )

    from tests._helpers import collect_tools

    tools = collect_tools(LucidProvider())
    out = await tools["get_lucid_user_profile"]()
    assert route.called
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer AT"
    assert req.headers["Lucid-Api-Version"] == "1"
    assert "Alice" in out
    assert "acc-1" in out


@respx.mock
async def test_get_lucid_document_contents_uses_special_accept_header(set_user_context, temp_db):
    set_user_context(email="alice@example.com", token="AT", scopes=["lucidchart.document.content"])
    await temp_db.save_tokens(
        "alice@example.com", "lucid", "AT", "RT", ["lucidchart.document.content"], ""
    )

    route = respx.get("https://api.lucid.co/documents/DOC/contents").mock(
        return_value=httpx.Response(
            200,
            json={
                "title": "Brainstorm",
                "pages": [
                    {"title": "P1", "items": [{"text": "Hello world"}, {"text": "second note"}]}
                ],
            },
        )
    )

    from tests._helpers import collect_tools

    tools = collect_tools(LucidProvider())
    out = await tools["get_lucid_document_contents"](document_id="DOC")
    assert route.called
    req = route.calls.last.request
    assert req.headers["Accept"] == "application/vnd.lucid.contents+json"
    assert req.headers["Lucid-Api-Version"] == "1"
    assert "Brainstorm" in out
    assert "Hello world" in out


@respx.mock
async def test_search_lucid_documents_posts_json(set_user_context, temp_db):
    set_user_context(email="alice@example.com", token="AT", scopes=["lucidchart.document.content"])
    await temp_db.save_tokens(
        "alice@example.com", "lucid", "AT", "RT", ["lucidchart.document.content"], ""
    )

    route = respx.post("https://api.lucid.co/documents/search").mock(
        return_value=httpx.Response(
            200,
            json=[{"title": "Spec", "documentId": "d-1", "product": "lucidchart"}],
        )
    )
    from tests._helpers import collect_tools

    tools = collect_tools(LucidProvider())
    out = await tools["search_lucid_documents"](keyword="spec")

    assert route.called
    req = route.calls.last.request
    body = json.loads(req.read().decode())
    assert body["keywords"] == "spec"
    assert "lucidchart" in body["product"]
    assert "lucidspark" in body["product"]
    assert "Spec" in out
    assert "d-1" in out


async def test_lucid_tool_requires_token(set_user_context):
    set_user_context(email="alice@example.com", host="proxy", token="", scopes=[])
    from tests._helpers import collect_tools

    tools = collect_tools(LucidProvider())
    out = await tools["get_lucid_user_profile"]()
    assert "[ACTION REQUIRED]" in out
    # Passthrough mode: tell GE to re-OAuth; no proxy-side /auth URL.
    assert "/auth/" not in out
