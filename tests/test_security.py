"""
Security tests for the WIF identity middleware in server.py.

Verifies inbound-token gating: missing Bearer, invalid Google token, audience
mismatch, missing email claim, and the public-route bypass.
"""

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

# Import server lazily inside fixtures so conftest env vars are already set.


@pytest.fixture
def client():
    """Use TestClient as a context manager so FastMCP's lifespan starts up
    (needed for /mcp/ requests to find an initialized session manager)."""
    import server

    with TestClient(server.app) as c:
        yield c


def test_health_is_public(client):
    """/health must bypass the WIF middleware entirely."""
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"
    assert body["active_provider"] == "miro"


def test_auth_routes_are_public(client):
    """The /auth/{provider} consent route must be reachable without a Bearer token."""
    # Following=False so we can observe the 307 to Miro's OAuth host.
    resp = client.get("/auth/miro?user=alice@example.com", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert "miro.com/oauth/authorize" in resp.headers["location"]


def test_protected_route_rejects_missing_bearer(client):
    resp = client.post("/mcp/", json={})
    assert resp.status_code == 401
    assert "Missing WIF Bearer token" in resp.text


@respx.mock
def test_protected_route_rejects_invalid_google_token(client):
    """If Google's tokeninfo returns non-200, we must reply 401."""
    respx.get("https://oauth2.googleapis.com/tokeninfo").mock(
        return_value=httpx.Response(400, json={"error": "invalid_token"})
    )
    resp = client.post("/mcp/", json={}, headers={"Authorization": "Bearer fake"})
    assert resp.status_code == 401
    assert "Invalid or expired Google Token" in resp.text


@respx.mock
def test_protected_route_rejects_audience_mismatch(client):
    """If the token aud/azp doesn't match GOOGLE_CLIENT_ID, reject."""
    respx.get("https://oauth2.googleapis.com/tokeninfo").mock(
        return_value=httpx.Response(
            200,
            json={
                "aud": "someone-elses-client.apps.googleusercontent.com",
                "azp": "someone-elses-client.apps.googleusercontent.com",
                "email": "alice@example.com",
            },
        )
    )
    resp = client.post("/mcp/", json={}, headers={"Authorization": "Bearer fake"})
    assert resp.status_code == 401
    assert "Audience" in resp.text or "Authorized Party" in resp.text


@respx.mock
def test_protected_route_rejects_token_without_email(client):
    """A valid token that lacks an email claim must yield 403."""
    respx.get("https://oauth2.googleapis.com/tokeninfo").mock(
        return_value=httpx.Response(
            200,
            json={
                "aud": "1234-test.apps.googleusercontent.com",
                "azp": "1234-test.apps.googleusercontent.com",
                # email intentionally missing
            },
        )
    )
    resp = client.post("/mcp/", json={}, headers={"Authorization": "Bearer fake"})
    assert resp.status_code == 403
    assert "email" in resp.text


@respx.mock
def test_protected_route_passes_middleware_with_valid_token(client):
    """A valid token must make it past the WIF middleware.

    We don't assert the downstream MCP response shape (that's FastMCP's concern);
    we only assert the middleware didn't itself return 401/403.
    """
    respx.get("https://oauth2.googleapis.com/tokeninfo").mock(
        return_value=httpx.Response(
            200,
            json={
                "aud": "1234-test.apps.googleusercontent.com",
                "azp": "1234-test.apps.googleusercontent.com",
                "email": "alice@example.com",
            },
        )
    )
    resp = client.post(
        "/mcp/",
        json={},
        headers={
            "Authorization": "Bearer fake",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
    )
    # FastMCP may answer 400/406 for a malformed protocol payload, but it must NOT
    # be the middleware that rejected with 401/403.
    assert resp.status_code not in (401, 403)
