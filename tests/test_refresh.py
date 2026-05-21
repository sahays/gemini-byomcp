"""
Passthrough-mode tests for call_with_refresh.

In Option A we don't store refresh tokens — Gemini Enterprise owns the OAuth
lifecycle. So `call_with_refresh` is now a thin wrapper:
  - 2xx → return as-is
  - 401 → raise RefreshFailedException (auth_boundary surfaces it to GE)
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
    @property
    def name(self):
        return "miro"


async def test_no_refresh_on_2xx(set_user_context):
    set_user_context(email="alice@example.com", token="any-token")

    async def do_request(token):
        assert token == "any-token"
        return httpx.Response(200, json={"ok": True})

    response = await call_with_refresh(FakeProvider(), do_request)
    assert response.status_code == 200


async def test_passthrough_does_not_attempt_refresh_on_401(set_user_context):
    """The proxy must NOT try to refresh in passthrough mode — GE owns OAuth."""
    set_user_context(email="alice@example.com", token="stale-token")
    call_count = {"n": 0}

    async def do_request(token):
        call_count["n"] += 1
        return httpx.Response(401)

    with pytest.raises(RefreshFailedException):
        await call_with_refresh(FakeProvider(), do_request)
    # The request must be made exactly once — no retry.
    assert call_count["n"] == 1


# compute_expires_at / is_expired still ship as utilities even though
# passthrough mode doesn't use them. Keep the unit tests so we notice if
# they regress when merging back to main.
async def test_compute_expires_at_subtracts_safety_margin():
    iso = compute_expires_at(3600)
    expiry = datetime.fromisoformat(iso)
    now = datetime.now(tz=UTC)
    delta = (expiry - now).total_seconds()
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
