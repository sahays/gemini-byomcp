"""
Tests for `call_with_refresh`: retry-once-on-401 semantics, persistence of
rotated tokens, and refresh-failure surfacing.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from providers.base import (
    RefreshFailedException,
    call_with_refresh,
    compute_expires_at,
    is_expired,
)


class FakeProvider:
    """Minimal provider stub for testing call_with_refresh in isolation."""

    def __init__(self, name="miro", refresh_payload=None, refresh_raises=False):
        self._name = name
        self.refresh_calls = []
        self._refresh_payload = refresh_payload or {
            "access_token": "new-token",
            "refresh_token": "new-refresh",
            "scopes": ["boards:read"],
            "expires_at": "",
        }
        self._refresh_raises = refresh_raises

    @property
    def name(self):
        return self._name

    async def refresh_access_token(self, refresh_token):
        self.refresh_calls.append(refresh_token)
        if self._refresh_raises:
            raise ValueError("refresh blew up")
        return self._refresh_payload


async def test_no_refresh_on_2xx(set_user_context, temp_db):
    set_user_context(email="alice@example.com", token="old-token")
    provider = FakeProvider()

    async def do_request(token):
        # Sanity-check the token threaded through is the live one
        assert token == "old-token"
        return httpx.Response(200, json={"ok": True})

    response = await call_with_refresh(provider, do_request)
    assert response.status_code == 200
    assert provider.refresh_calls == []  # no refresh happened


async def test_refresh_on_401_and_retry(set_user_context, temp_db):
    set_user_context(email="alice@example.com", token="stale-token", scopes=["boards:read"])
    # Seed DB with a refresh token for this user+provider
    await temp_db.save_tokens(
        email="alice@example.com",
        provider="miro",
        access_token="stale-token",
        refresh_token="r-1",
        scopes=["boards:read"],
        expires_at="",
    )

    provider = FakeProvider(name="miro")

    call_log = []

    async def do_request(token):
        call_log.append(token)
        if token == "stale-token":
            return httpx.Response(401, json={"error": "expired"})
        return httpx.Response(200, json={"ok": True})

    response = await call_with_refresh(provider, do_request)
    assert response.status_code == 200
    assert provider.refresh_calls == ["r-1"]
    assert call_log == ["stale-token", "new-token"]

    # New tokens persisted
    info = await temp_db.get_token_info("alice@example.com", "miro")
    assert info["access_token"] == "new-token"
    assert info["refresh_token"] == "new-refresh"


async def test_refresh_preserves_old_refresh_token_when_provider_omits_it(
    set_user_context, temp_db
):
    set_user_context(email="alice@example.com", token="stale-token")
    await temp_db.save_tokens(
        email="alice@example.com",
        provider="miro",
        access_token="stale-token",
        refresh_token="r-original",
        scopes=["boards:read"],
        expires_at="",
    )

    # Provider returns no refresh_token in refresh response — common for Figma
    provider = FakeProvider(
        refresh_payload={
            "access_token": "new-token",
            "refresh_token": "",
            "scopes": [],
            "expires_at": "",
        }
    )

    async def do_request(token):
        return httpx.Response(401) if token == "stale-token" else httpx.Response(200)

    await call_with_refresh(provider, do_request)
    info = await temp_db.get_token_info("alice@example.com", "miro")
    assert info["refresh_token"] == "r-original"  # preserved


async def test_refresh_failure_when_no_refresh_token_stored(set_user_context, temp_db):
    set_user_context(email="alice@example.com", token="stale-token")
    # No save_tokens — DB has nothing for this user

    provider = FakeProvider()

    async def do_request(token):
        return httpx.Response(401)

    with pytest.raises(RefreshFailedException):
        await call_with_refresh(provider, do_request)
    assert provider.refresh_calls == []  # never tried


async def test_refresh_failure_when_provider_throws(set_user_context, temp_db):
    set_user_context(email="alice@example.com", token="stale-token")
    await temp_db.save_tokens(
        email="alice@example.com",
        provider="miro",
        access_token="stale-token",
        refresh_token="r-1",
        scopes=[],
        expires_at="",
    )

    provider = FakeProvider(refresh_raises=True)

    async def do_request(token):
        return httpx.Response(401)

    with pytest.raises(RefreshFailedException):
        await call_with_refresh(provider, do_request)


async def test_compute_expires_at_subtracts_safety_margin():
    iso = compute_expires_at(3600)  # 1 hour
    expiry = datetime.fromisoformat(iso)
    now = datetime.now(tz=UTC)
    delta = (expiry - now).total_seconds()
    # ~3540s (3600 - 60 safety margin), give it a wide window for slow CI
    assert 3500 < delta < 3600


async def test_compute_expires_at_handles_none():
    assert compute_expires_at(None) == ""
    assert compute_expires_at(0) == ""


async def test_is_expired_true_when_past():
    past = (datetime.now(tz=UTC) - timedelta(minutes=5)).isoformat()
    assert is_expired(past) is True


async def test_is_expired_false_when_future():
    future = (datetime.now(tz=UTC) + timedelta(hours=1)).isoformat()
    assert is_expired(future) is False


async def test_is_expired_false_for_empty_or_malformed():
    assert is_expired("") is False
    assert is_expired("not-a-date") is False
