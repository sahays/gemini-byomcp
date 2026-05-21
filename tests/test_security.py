"""
Passthrough-mode security tests.

The middleware in this branch doesn't validate inbound Google tokens (Gemini
Enterprise owns the OAuth flow); it just enforces that an Authorization: Bearer
header is present and forwards it via ContextVars to the tools.
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Use TestClient as a context manager so FastMCP's lifespan starts up
    (needed for /mcp/ requests to find an initialized session manager)."""
    import server

    with TestClient(server.app) as c:
        yield c


def test_health_is_public(client):
    """/health bypasses the bearer-token requirement."""
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"
    assert body["mode"] == "passthrough"
    assert body["active_provider"] == "miro"


def test_protected_route_rejects_missing_bearer(client):
    """Without a Bearer header, /mcp returns 401 and a WWW-Authenticate challenge."""
    resp = client.post("/mcp/", json={})
    assert resp.status_code == 401
    assert "Missing Bearer token" in resp.text
    assert resp.headers.get("WWW-Authenticate", "").startswith("Bearer")


def test_protected_route_accepts_any_bearer(client):
    """The proxy doesn't validate the token itself — it forwards to the SaaS,
    which is the authority on whether the token is valid.

    A malformed/test token therefore passes the middleware but the downstream
    MCP layer may still 400 the request (we only assert it's not 401)."""
    resp = client.post(
        "/mcp/",
        json={},
        headers={
            "Authorization": "Bearer test-passthrough-token",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code != 401


def test_no_auth_routes_in_passthrough(client):
    """Option-A drops the /auth/{provider} consent and callback routes; GE
    handles OAuth instead. Even with a bearer present so we get past the
    middleware, the routes themselves must be gone."""
    headers = {"Authorization": "Bearer test"}
    assert client.get("/auth/miro?user=alice@example.com", headers=headers).status_code == 404
    assert client.get("/auth/miro/callback?code=x&state=y", headers=headers).status_code == 404
