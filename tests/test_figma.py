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
    # Only scopes selectable in standard Figma apps. Enterprise-tier scopes
    # (file_variables:*, file_dev_resources:write, library_analytics:read),
    # selections:read (no REST), and projects:*/webhooks:* are intentionally
    # excluded.
    for scope_token in (
        "current_user%3Aread",
        "file_content%3Aread",
        "file_metadata%3Aread",
        "file_comments%3Aread",
        "file_comments%3Awrite",
        "file_dev_resources%3Aread",
        "file_versions%3Aread",
        "library_content%3Aread",
        "library_assets%3Aread",
        "team_library_content%3Aread",
    ):
        assert scope_token in url, f"Missing {scope_token} from Figma consent URL"
    for excluded in (
        "selections%3Aread",
        "projects%3Aread",
        "project_metadata%3Aread",
        "file_variables%3Aread",
        "file_variables%3Awrite",
        "file_dev_resources%3Awrite",
        "library_analytics%3Aread",
        "webhooks%3Aread",
        "webhooks%3Awrite",
    ):
        assert excluded not in url, f"Unexpected scope {excluded} in consent URL"


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
async def test_get_figma_team_components_requires_team_library_scope(set_user_context, temp_db):
    set_user_context(
        email="alice@example.com",
        token="AT",
        scopes=["file_content:read"],  # missing team_library_content:read
        host="proxy",
    )
    await temp_db.save_tokens("alice@example.com", "figma", "AT", "RT", ["file_content:read"], "")

    from tests._helpers import collect_tools

    tools = collect_tools(FigmaProvider())
    out = await tools["get_figma_team_components"](team_id="T")
    assert "[PERMISSION REQUIRED]" in out
    assert "team_library_content:read" in out


@respx.mock
async def test_get_figma_team_components_happy_path(set_user_context, temp_db):
    set_user_context(
        email="alice@example.com", token="AT", scopes=["team_library_content:read"], host="proxy"
    )
    await temp_db.save_tokens(
        "alice@example.com", "figma", "AT", "RT", ["team_library_content:read"], ""
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


# --- New tools covering the rest of the scope surface -----------------------


@respx.mock
async def test_get_figma_me_zero_args(set_user_context, temp_db):
    set_user_context(email="alice@example.com", token="AT", scopes=["current_user:read"])
    await temp_db.save_tokens("alice@example.com", "figma", "AT", "RT", ["current_user:read"], "")
    respx.get("https://api.figma.com/v1/me").mock(
        return_value=httpx.Response(
            200, json={"id": "u1", "handle": "alice", "email": "alice@example.com"}
        )
    )
    from tests._helpers import collect_tools

    tools = collect_tools(FigmaProvider())
    out = await tools["get_figma_me"]()
    assert "alice" in out


@respx.mock
async def test_get_figma_file_metadata(set_user_context, temp_db):
    set_user_context(email="alice@example.com", token="AT", scopes=["file_metadata:read"])
    await temp_db.save_tokens("alice@example.com", "figma", "AT", "RT", ["file_metadata:read"], "")
    respx.get("https://api.figma.com/v1/files/FK/meta").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "Mockup",
                "last_modified": "2026-05-22T00:00:00Z",
                "editor_type": "figma",
                "role": "editor",
            },
        )
    )
    from tests._helpers import collect_tools

    tools = collect_tools(FigmaProvider())
    out = await tools["get_figma_file_metadata"](file_key="FK")
    assert "Mockup" in out
    assert "editor" in out


@respx.mock
async def test_render_figma_file_images(set_user_context, temp_db):
    set_user_context(email="alice@example.com", token="AT", scopes=["file_content:read"])
    await temp_db.save_tokens("alice@example.com", "figma", "AT", "RT", ["file_content:read"], "")
    respx.get("https://api.figma.com/v1/images/FK?ids=1%3A2&format=png").mock(
        return_value=httpx.Response(200, json={"images": {"1:2": "https://cdn/x.png"}})
    )
    from tests._helpers import collect_tools

    tools = collect_tools(FigmaProvider())
    out = await tools["render_figma_file_images"](file_key="FK", node_ids="1:2")
    assert "https://cdn/x.png" in out


@respx.mock
async def test_get_figma_file_image_fills(set_user_context, temp_db):
    set_user_context(email="alice@example.com", token="AT", scopes=["file_content:read"])
    await temp_db.save_tokens("alice@example.com", "figma", "AT", "RT", ["file_content:read"], "")
    respx.get("https://api.figma.com/v1/files/FK/images").mock(
        return_value=httpx.Response(200, json={"meta": {"images": {"img1": "https://cdn/a.png"}}})
    )
    from tests._helpers import collect_tools

    tools = collect_tools(FigmaProvider())
    out = await tools["get_figma_file_image_fills"](file_key="FK")
    assert "https://cdn/a.png" in out


@respx.mock
async def test_get_figma_file_comments(set_user_context, temp_db):
    set_user_context(email="alice@example.com", token="AT", scopes=["file_comments:read"])
    await temp_db.save_tokens("alice@example.com", "figma", "AT", "RT", ["file_comments:read"], "")
    respx.get("https://api.figma.com/v1/files/FK/comments").mock(
        return_value=httpx.Response(
            200,
            json={
                "comments": [
                    {"id": "c1", "message": "Tighten kerning", "user": {"handle": "alice"}}
                ]
            },
        )
    )
    from tests._helpers import collect_tools

    tools = collect_tools(FigmaProvider())
    out = await tools["get_figma_file_comments"](file_key="FK")
    assert "Tighten kerning" in out


@respx.mock
async def test_post_figma_file_comment(set_user_context, temp_db):
    set_user_context(email="alice@example.com", token="AT", scopes=["file_comments:write"])
    await temp_db.save_tokens("alice@example.com", "figma", "AT", "RT", ["file_comments:write"], "")
    route = respx.post("https://api.figma.com/v1/files/FK/comments").mock(
        return_value=httpx.Response(201, json={"id": "c-new"})
    )
    from tests._helpers import collect_tools

    tools = collect_tools(FigmaProvider())
    out = await tools["post_figma_file_comment"](file_key="FK", message="LGTM")
    assert route.called
    body = route.calls.last.request.read().decode()
    assert "LGTM" in body
    assert "c-new" in out


@respx.mock
async def test_get_figma_file_versions(set_user_context, temp_db):
    set_user_context(email="alice@example.com", token="AT", scopes=["file_versions:read"])
    await temp_db.save_tokens("alice@example.com", "figma", "AT", "RT", ["file_versions:read"], "")
    respx.get("https://api.figma.com/v1/files/FK/versions").mock(
        return_value=httpx.Response(
            200,
            json={
                "versions": [
                    {
                        "id": "v1",
                        "created_at": "2026-05-01T00:00:00Z",
                        "label": "v1.0",
                        "user": {"handle": "alice"},
                    }
                ]
            },
        )
    )
    from tests._helpers import collect_tools

    tools = collect_tools(FigmaProvider())
    out = await tools["get_figma_file_versions"](file_key="FK")
    assert "v1.0" in out


@respx.mock
async def test_get_figma_file_dev_resources(set_user_context, temp_db):
    set_user_context(email="alice@example.com", token="AT", scopes=["file_dev_resources:read"])
    await temp_db.save_tokens(
        "alice@example.com", "figma", "AT", "RT", ["file_dev_resources:read"], ""
    )
    respx.get("https://api.figma.com/v1/files/FK/dev_resources").mock(
        return_value=httpx.Response(
            200,
            json={
                "dev_resources": [
                    {"name": "PR-42", "url": "https://github.com/x/x/pull/42", "node_id": "1:2"}
                ]
            },
        )
    )
    from tests._helpers import collect_tools

    tools = collect_tools(FigmaProvider())
    out = await tools["get_figma_file_dev_resources"](file_key="FK")
    assert "PR-42" in out


@respx.mock
async def test_get_figma_file_components(set_user_context, temp_db):
    set_user_context(email="alice@example.com", token="AT", scopes=["library_content:read"])
    await temp_db.save_tokens(
        "alice@example.com", "figma", "AT", "RT", ["library_content:read"], ""
    )
    respx.get("https://api.figma.com/v1/files/FK/components").mock(
        return_value=httpx.Response(
            200, json={"meta": {"components": [{"name": "Button", "key": "k1"}]}}
        )
    )
    from tests._helpers import collect_tools

    tools = collect_tools(FigmaProvider())
    out = await tools["get_figma_file_components"](file_key="FK")
    assert "Button" in out


@respx.mock
async def test_get_figma_file_styles(set_user_context, temp_db):
    set_user_context(email="alice@example.com", token="AT", scopes=["library_content:read"])
    await temp_db.save_tokens(
        "alice@example.com", "figma", "AT", "RT", ["library_content:read"], ""
    )
    respx.get("https://api.figma.com/v1/files/FK/styles").mock(
        return_value=httpx.Response(
            200,
            json={"meta": {"styles": [{"name": "Primary", "style_type": "FILL", "key": "s1"}]}},
        )
    )
    from tests._helpers import collect_tools

    tools = collect_tools(FigmaProvider())
    out = await tools["get_figma_file_styles"](file_key="FK")
    assert "Primary" in out


@respx.mock
async def test_get_figma_team_styles(set_user_context, temp_db):
    set_user_context(email="alice@example.com", token="AT", scopes=["team_library_content:read"])
    await temp_db.save_tokens(
        "alice@example.com", "figma", "AT", "RT", ["team_library_content:read"], ""
    )
    respx.get("https://api.figma.com/v1/teams/TEAM/styles?page_size=30").mock(
        return_value=httpx.Response(
            200,
            json={"meta": {"styles": [{"name": "BodyText", "style_type": "TEXT", "key": "s2"}]}},
        )
    )
    from tests._helpers import collect_tools

    tools = collect_tools(FigmaProvider())
    out = await tools["get_figma_team_styles"](team_id="TEAM")
    assert "BodyText" in out


@respx.mock
async def test_get_figma_component(set_user_context, temp_db):
    set_user_context(email="alice@example.com", token="AT", scopes=["library_assets:read"])
    await temp_db.save_tokens("alice@example.com", "figma", "AT", "RT", ["library_assets:read"], "")
    respx.get("https://api.figma.com/v1/components/KEY").mock(
        return_value=httpx.Response(
            200, json={"meta": {"name": "Button", "key": "KEY", "file_key": "FK"}}
        )
    )
    from tests._helpers import collect_tools

    tools = collect_tools(FigmaProvider())
    out = await tools["get_figma_component"](key="KEY")
    assert "Button" in out


@respx.mock
async def test_get_figma_style(set_user_context, temp_db):
    set_user_context(email="alice@example.com", token="AT", scopes=["library_assets:read"])
    await temp_db.save_tokens("alice@example.com", "figma", "AT", "RT", ["library_assets:read"], "")
    respx.get("https://api.figma.com/v1/styles/SK").mock(
        return_value=httpx.Response(
            200,
            json={"meta": {"name": "Primary", "style_type": "FILL", "key": "SK", "file_key": "FK"}},
        )
    )
    from tests._helpers import collect_tools

    tools = collect_tools(FigmaProvider())
    out = await tools["get_figma_style"](key="SK")
    assert "Primary" in out
