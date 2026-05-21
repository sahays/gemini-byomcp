"""
Passthrough-mode tests for require_scopes + auth_boundary.

In Option A there's no /auth/* URL to send the user to, so the boundary
returns a brief "ask GE to re-OAuth the user" instruction instead.
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
    set_user_context(token="", scopes=[])

    @auth_boundary("miro")
    @require_scopes(["boards:read"])
    async def tool():
        return "should-not-reach"

    out = await tool()
    assert "[ACTION REQUIRED]" in out
    assert "bearer token" in out.lower()
    # Passthrough: no /auth/miro URL should appear.
    assert "/auth/" not in out


async def test_auth_boundary_handles_missing_scope(set_user_context):
    set_user_context(token="tok", scopes=["boards:read"])

    @auth_boundary("miro")
    @require_scopes(["boards:write"])
    async def tool():
        return "should-not-reach"

    out = await tool()
    assert "[PERMISSION REQUIRED]" in out
    assert "boards:write" in out
    assert "not granted" in out
    # Mentions the GE data-source config, not a proxy /auth URL.
    assert "Gemini Enterprise" in out


async def test_auth_boundary_handles_refresh_failure(set_user_context):
    set_user_context(token="tok")

    @auth_boundary("lucid")
    async def tool():
        raise RefreshFailedException("nope")

    out = await tool()
    assert "[ACTION REQUIRED]" in out
    assert "rejected" in out.lower() or "expired" in out.lower()
    assert "/auth/" not in out


async def test_auth_boundary_swallows_unexpected_errors(set_user_context):
    set_user_context()

    @auth_boundary("miro")
    async def tool():
        raise RuntimeError("boom")

    out = await tool()
    assert out.startswith("Error:")
    assert "boom" in out
