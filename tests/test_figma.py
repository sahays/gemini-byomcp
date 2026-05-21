"""
Figma provider tests — Basic-auth header on token endpoint, correct refresh URL,
and downstream Figma REST calls.
"""

import base64

import httpx
import pytest
import respx

from providers.figma import FigmaProvider


def test_get_auth_url_includes_scopes_and_state():
    provider = FigmaProvider()
    url = provider.get_auth_url("alice@example.com")
    assert url.startswith("https://www.figma.com/oauth?")
    assert "client_id=figma-test-id" in url
    assert "response_type=code" in url
    assert "state=alice@example.com" in url
    assert "redirect_uri=http://localhost:8080/auth/figma/callback" in url
    # Every non-admin scope must be in the consent URL (URL-encoded `:` → %3A).
    for scope_token in (
        "current_user%3Aread",
        "file_content%3Aread",
        "file_metadata%3Aread",
        "file_comments%3Aread",
        "file_comments%3Awrite",
        "file_dev_resources%3Aread",
        "file_dev_resources%3Awrite",
        "file_variables%3Aread",
        "file_variables%3Awrite",
        "file_versions%3Aread",
        "library_content%3Aread",
        "library_assets%3Aread",
        "library_analytics%3Aread",
        "team_library_content%3Aread",
        "selections%3Aread",
        "projects%3Aread",
        "project_metadata%3Aread",
        "webhooks%3Aread",
        "webhooks%3Awrite",
    ):
        assert scope_token in url, f"Missing {scope_token} from Figma consent URL"


@respx.mock
async def test_exchange_code_uses_basic_auth():
    provider = FigmaProvider()
    route = respx.post("https://api.figma.com/v1/oauth/token").mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "AT", "refresh_token": "RT", "expires_in": 7776000},
        )
    )

    result = await provider.exchange_code_for_tokens(
        "c", "http://localhost:8080/auth/figma/callback"
    )
    assert route.called
    req = route.calls.last.request

    expected_auth = "Basic " + base64.b64encode(b"figma-test-id:figma-test-secret").decode()
    assert req.headers["Authorization"] == expected_auth

    body = req.read().decode()
    assert "grant_type=authorization_code" in body
    assert "code=c" in body
    assert "redirect_uri=http" in body  # url-encoded redirect

    assert result["access_token"] == "AT"
    assert result["refresh_token"] == "RT"
    assert result["expires_at"]  # ISO timestamp present
    # Figma doesn't return granted scopes — provider falls back to requested defaults
    assert "file_content:read" in result["scopes"]
    assert "library_content:read" in result["scopes"]


@respx.mock
async def test_refresh_uses_refresh_endpoint_not_token_endpoint():
    provider = FigmaProvider()
    route = respx.post("https://api.figma.com/v1/oauth/refresh").mock(
        return_value=httpx.Response(200, json={"access_token": "NEW", "expires_in": 7776000})
    )
    result = await provider.refresh_access_token("OLD-R")
    assert route.called
    expected_auth = "Basic " + base64.b64encode(b"figma-test-id:figma-test-secret").decode()
    assert route.calls.last.request.headers["Authorization"] == expected_auth
    assert result["access_token"] == "NEW"
    # Figma refresh omits refresh_token; provider should preserve the original
    assert result["refresh_token"] == "OLD-R"


@respx.mock
async def test_exchange_failure_raises():
    provider = FigmaProvider()
    respx.post("https://api.figma.com/v1/oauth/token").mock(
        return_value=httpx.Response(400, json={"error": "bad"})
    )
    with pytest.raises(ValueError):
        await provider.exchange_code_for_tokens("c", "http://x")


@respx.mock
async def test_get_figma_file_uses_depth_param(set_user_context, temp_db):
    set_user_context(email="alice@example.com", token="AT", scopes=["file_content:read"])
    await temp_db.save_tokens("alice@example.com", "figma", "AT", "RT", ["file_content:read"], "")

    route = respx.get("https://api.figma.com/v1/files/FILEKEY?depth=1").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "Sample File",
                "lastModified": "2026-05-01T00:00:00Z",
                "document": {"children": [{"name": "Page 1"}, {"name": "Page 2"}]},
            },
        )
    )

    from tests._helpers import collect_tools

    tools = collect_tools(FigmaProvider())
    out = await tools["get_figma_file"](file_key="FILEKEY")

    assert route.called
    assert route.calls.last.request.headers["Authorization"] == "Bearer AT"
    assert "Sample File" in out
    assert "Page 1" in out
    assert "Page 2" in out


@respx.mock
async def test_get_figma_file_nodes_uses_ids_query(set_user_context, temp_db):
    set_user_context(email="alice@example.com", token="AT", scopes=["file_content:read"])
    await temp_db.save_tokens("alice@example.com", "figma", "AT", "RT", ["file_content:read"], "")

    route = respx.get("https://api.figma.com/v1/files/FK/nodes").mock(
        return_value=httpx.Response(
            200,
            json={"nodes": {"1:2": {"document": {"type": "FRAME", "name": "Header"}}}},
        )
    )

    from tests._helpers import collect_tools

    tools = collect_tools(FigmaProvider())
    out = await tools["get_figma_file_nodes"](file_key="FK", node_ids="1:2")

    assert route.called
    req = route.calls.last.request
    # node IDs should be URL-encoded into the ids query param
    assert "ids=1%3A2" in str(req.url)
    assert "FRAME" in out
    assert "Header" in out


@respx.mock
async def test_get_figma_team_components_requires_library_scope(set_user_context, temp_db):
    set_user_context(
        email="alice@example.com",
        token="AT",
        scopes=["file_content:read"],  # missing library_content:read
        host="proxy",
    )
    await temp_db.save_tokens("alice@example.com", "figma", "AT", "RT", ["file_content:read"], "")

    from tests._helpers import collect_tools

    tools = collect_tools(FigmaProvider())
    out = await tools["get_figma_team_components"](team_id="T")
    # Should NOT have hit the API
    assert "[PERMISSION REQUIRED]" in out
    assert "library_content:read" in out


@respx.mock
async def test_get_figma_team_components_happy_path(set_user_context, temp_db):
    set_user_context(
        email="alice@example.com", token="AT", scopes=["library_content:read"], host="proxy"
    )
    await temp_db.save_tokens(
        "alice@example.com", "figma", "AT", "RT", ["library_content:read"], ""
    )

    route = respx.get("https://api.figma.com/v1/teams/TEAM/components?page_size=30").mock(
        return_value=httpx.Response(
            200, json={"meta": {"components": [{"name": "Button", "key": "k1"}]}}
        )
    )
    from tests._helpers import collect_tools

    tools = collect_tools(FigmaProvider())
    out = await tools["get_figma_team_components"](team_id="TEAM")
    assert route.called
    assert "Button" in out
    assert "k1" in out
