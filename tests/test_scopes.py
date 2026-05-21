"""
Scope-enforcement tests.

Covers `require_scopes` (raises) and `auth_boundary` (translates exceptions
into user-friendly prompts).
"""

import pytest

from providers.base import (
    MissingScopeException,
    RefreshFailedException,
    auth_boundary,
    require_scopes,
)


async def test_require_scopes_raises_when_no_token(set_user_context):
    set_user_context(token="", scopes=[])

    @require_scopes(["boards:read"])
    async def tool():
        return "ok"

    with pytest.raises(MissingScopeException) as excinfo:
        await tool()
    assert excinfo.value.active_scopes == []
    assert "boards:read" in excinfo.value.required_scopes


async def test_require_scopes_raises_when_missing_scope(set_user_context):
    set_user_context(token="tok", scopes=["boards:read"])

    @require_scopes(["boards:read", "boards:write"])
    async def tool():
        return "ok"

    with pytest.raises(MissingScopeException) as excinfo:
        await tool()
    assert "boards:write" in excinfo.value.required_scopes
    assert excinfo.value.active_scopes == ["boards:read"]


async def test_require_scopes_passes_when_all_present(set_user_context):
    set_user_context(token="tok", scopes=["boards:read", "boards:write"])

    @require_scopes(["boards:read"])
    async def tool():
        return "ok"

    assert await tool() == "ok"


async def test_auth_boundary_handles_no_token(set_user_context):
    set_user_context(email="alice@example.com", host="proxy.example.com", token="", scopes=[])

    @auth_boundary("miro")
    @require_scopes(["boards:read"])
    async def tool():
        return "should-not-reach"

    out = await tool()
    assert "[ACTION REQUIRED]" in out
    assert "https://proxy.example.com/auth/miro?user=alice@example.com" in out


async def test_auth_boundary_handles_missing_scope(set_user_context):
    set_user_context(
        email="alice@example.com", host="proxy.example.com", token="tok", scopes=["boards:read"]
    )

    @auth_boundary("miro")
    @require_scopes(["boards:write"])
    async def tool():
        return "should-not-reach"

    out = await tool()
    assert "[PERMISSION REQUIRED]" in out
    assert "boards:write" in out
    assert "not granted" in out


async def test_auth_boundary_uses_http_on_localhost(set_user_context):
    set_user_context(email="alice@example.com", host="localhost:8080", token="", scopes=[])

    @auth_boundary("figma")
    @require_scopes(["file_content:read"])
    async def tool():
        return "x"

    out = await tool()
    assert "http://localhost:8080/auth/figma?user=alice@example.com" in out


async def test_auth_boundary_handles_refresh_failure(set_user_context):
    set_user_context(email="alice@example.com", host="proxy.example.com", token="tok")

    @auth_boundary("lucid")
    async def tool():
        raise RefreshFailedException("nope")

    out = await tool()
    assert "[ACTION REQUIRED]" in out
    assert "expired" in out.lower() or "re-link" in out.lower()
    assert "/auth/lucid?user=alice@example.com" in out


async def test_auth_boundary_swallows_unexpected_errors(set_user_context):
    set_user_context()

    @auth_boundary("miro")
    async def tool():
        raise RuntimeError("boom")

    out = await tool()
    assert out.startswith("Error:")
    assert "boom" in out
