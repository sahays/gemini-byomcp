"""
Miro provider tests — OAuth URL shape, token exchange/refresh wire format,
and exact downstream Miro REST API calls.
"""

import httpx
import pytest
import respx

from providers.miro import MiroProvider


def test_get_auth_url_contains_required_params():
    provider = MiroProvider()
    url = provider.get_auth_url("alice@example.com")
    assert url.startswith("https://miro.com/oauth/authorize?")
    assert "response_type=code" in url
    assert "client_id=miro-test-id" in url
    assert "redirect_uri=http://localhost:8080/auth/miro/callback" in url
    assert "state=alice@example.com" in url
    # Every non-admin scope must be included in the consent URL.
    for scope_token in (
        "boards%3Aread",
        "boards%3Awrite",
        "boards%3Aexport",
        "identity%3Aread",
        "projects%3Aread",
        "projects%3Awrite",
    ):
        assert scope_token in url, f"Missing {scope_token} from Miro consent URL"


@respx.mock
async def test_exchange_code_for_tokens_hits_correct_endpoint():
    provider = MiroProvider()
    route = respx.post("https://api.miro.com/v1/oauth/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "AT",
                "refresh_token": "RT",
                "scope": "boards:read boards:write",
                "expires_in": 3600,
            },
        )
    )

    result = await provider.exchange_code_for_tokens(
        code="auth-code", redirect_uri="http://localhost:8080/auth/miro/callback"
    )

    assert route.called
    request = route.calls.last.request
    qs = dict(request.url.params)
    assert qs["grant_type"] == "authorization_code"
    assert qs["client_id"] == "miro-test-id"
    assert qs["client_secret"] == "miro-test-secret"
    assert qs["code"] == "auth-code"
    assert qs["redirect_uri"] == "http://localhost:8080/auth/miro/callback"

    assert result["access_token"] == "AT"
    assert result["refresh_token"] == "RT"
    assert result["scopes"] == ["boards:read", "boards:write"]
    assert result["expires_at"]  # non-empty ISO timestamp


@respx.mock
async def test_exchange_code_failure_raises():
    provider = MiroProvider()
    respx.post("https://api.miro.com/v1/oauth/token").mock(
        return_value=httpx.Response(400, json={"error": "bad_code"})
    )
    with pytest.raises(ValueError):
        await provider.exchange_code_for_tokens("bad", "http://x")


@respx.mock
async def test_refresh_access_token_uses_grant_type_refresh():
    provider = MiroProvider()
    route = respx.post("https://api.miro.com/v1/oauth/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "NEW",
                "refresh_token": "NEW-R",
                "expires_in": 1800,
                "scope": "boards:read",
            },
        )
    )

    result = await provider.refresh_access_token("OLD-R")
    qs = dict(route.calls.last.request.url.params)
    assert qs["grant_type"] == "refresh_token"
    assert qs["refresh_token"] == "OLD-R"
    assert qs["client_id"] == "miro-test-id"
    assert qs["client_secret"] == "miro-test-secret"

    assert result["access_token"] == "NEW"
    assert result["refresh_token"] == "NEW-R"
    assert result["scopes"] == ["boards:read"]


@respx.mock
async def test_get_miro_board_items_calls_v2_items_endpoint(set_user_context, temp_db):
    """End-to-end-ish: ensure the tool calls the right Miro endpoint with a Bearer header."""
    set_user_context(email="alice@example.com", token="AT", scopes=["boards:read"])
    await temp_db.save_tokens("alice@example.com", "miro", "AT", "RT", ["boards:read"], "")

    route = respx.get("https://api.miro.com/v2/boards/BOARD123/items?limit=50").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"type": "sticky_note", "data": {"content": "<p>Hello</p>"}},
                    {"type": "text", "data": {"content": "<p>World</p>"}},
                ]
            },
        )
    )

    # Register tools onto a fake MCP, capture them
    from tests._helpers import collect_tools

    tools = collect_tools(MiroProvider())
    result = await tools["get_miro_board_items"](board_id="BOARD123")

    assert route.called
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer AT"
    assert "Hello" in result
    assert "World" in result


@respx.mock
async def test_create_miro_sticky_note_posts_payload(set_user_context, temp_db):
    set_user_context(email="alice@example.com", token="AT", scopes=["boards:write"])
    await temp_db.save_tokens("alice@example.com", "miro", "AT", "RT", ["boards:write"], "")

    route = respx.post("https://api.miro.com/v2/boards/B/items").mock(
        return_value=httpx.Response(201, json={"id": "item-1"})
    )

    from tests._helpers import collect_tools

    tools = collect_tools(MiroProvider())
    out = await tools["create_miro_sticky_note"](board_id="B", content="hello")

    assert route.called
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer AT"
    body = req.read().decode()
    assert "sticky_note" in body
    assert "hello" in body
    assert "item-1" in out


@respx.mock
async def test_delete_miro_board_item_uses_delete_method(set_user_context, temp_db):
    set_user_context(email="alice@example.com", token="AT", scopes=["boards:write"])
    await temp_db.save_tokens("alice@example.com", "miro", "AT", "RT", ["boards:write"], "")

    route = respx.delete("https://api.miro.com/v2/boards/B/items/I").mock(
        return_value=httpx.Response(204)
    )

    from tests._helpers import collect_tools

    tools = collect_tools(MiroProvider())
    out = await tools["delete_miro_board_item"](board_id="B", item_id="I")

    assert route.called
    assert "Successfully deleted" in out


async def test_get_miro_board_items_requires_token(set_user_context):
    """Without an active token, the tool must surface the [ACTION REQUIRED] auth link."""
    set_user_context(email="alice@example.com", host="proxy", token="", scopes=[])
    from tests._helpers import collect_tools

    tools = collect_tools(MiroProvider())
    out = await tools["get_miro_board_items"](board_id="X")
    assert "[ACTION REQUIRED]" in out
    assert "/auth/miro?user=alice@example.com" in out


async def test_create_sticky_note_requires_write_scope(set_user_context):
    """Read-only token must NOT allow writes."""
    set_user_context(email="alice@example.com", host="proxy", token="AT", scopes=["boards:read"])
    from tests._helpers import collect_tools

    tools = collect_tools(MiroProvider())
    out = await tools["create_miro_sticky_note"](board_id="X", content="y")
    assert "[PERMISSION REQUIRED]" in out
    assert "boards:write" in out
