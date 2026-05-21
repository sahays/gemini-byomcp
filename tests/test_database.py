"""
LocalJsonStore tests — concurrency-safe per-provider isolation + expires_at round-trip.
"""

import json

from database import LocalJsonStore


async def test_save_and_fetch_roundtrip(tmp_path):
    store = LocalJsonStore(filepath=str(tmp_path / "db.json"))
    ok = await store.save_tokens(
        email="alice@example.com",
        provider="miro",
        access_token="A",
        refresh_token="R",
        scopes=["boards:read", "boards:write"],
        expires_at="2030-01-01T00:00:00+00:00",
    )
    assert ok is True

    info = await store.get_token_info("alice@example.com", "miro")
    assert info == {
        "access_token": "A",
        "refresh_token": "R",
        "scopes": ["boards:read", "boards:write"],
        "expires_at": "2030-01-01T00:00:00+00:00",
    }


async def test_get_token_info_returns_none_when_missing(tmp_path):
    store = LocalJsonStore(filepath=str(tmp_path / "db.json"))
    assert await store.get_token_info("nobody@example.com", "miro") is None


async def test_multiple_providers_isolated_per_user(tmp_path):
    store = LocalJsonStore(filepath=str(tmp_path / "db.json"))
    await store.save_tokens("alice@example.com", "miro", "M-A", "M-R", ["boards:read"], "")
    await store.save_tokens("alice@example.com", "figma", "F-A", "F-R", ["file_content:read"], "")

    miro = await store.get_token_info("alice@example.com", "miro")
    figma = await store.get_token_info("alice@example.com", "figma")

    assert miro["access_token"] == "M-A"
    assert miro["scopes"] == ["boards:read"]
    assert figma["access_token"] == "F-A"
    assert figma["scopes"] == ["file_content:read"]


async def test_users_isolated(tmp_path):
    store = LocalJsonStore(filepath=str(tmp_path / "db.json"))
    await store.save_tokens("alice@example.com", "miro", "A1", "R1", [], "")
    await store.save_tokens("bob@example.com", "miro", "B1", "R2", [], "")

    assert (await store.get_token_info("alice@example.com", "miro"))["access_token"] == "A1"
    assert (await store.get_token_info("bob@example.com", "miro"))["access_token"] == "B1"


async def test_persisted_file_uses_namespaced_keys(tmp_path):
    """Token keys must be namespaced by provider so two SaaS for the same user don't collide."""
    path = tmp_path / "db.json"
    store = LocalJsonStore(filepath=str(path))
    await store.save_tokens("alice@example.com", "lucid", "L-A", "L-R", ["user.profile"], "exp")

    raw = json.loads(path.read_text())
    assert "alice@example.com" in raw
    record = raw["alice@example.com"]
    assert record["lucid_access_token"] == "L-A"
    assert record["lucid_refresh_token"] == "L-R"
    assert record["lucid_scopes"] == ["user.profile"]
    assert record["lucid_expires_at"] == "exp"
