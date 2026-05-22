"""
Shared pytest fixtures + environment bootstrap.

IMPORTANT: env vars are set at module import time so project modules
(config.py, providers/*) pick them up on their first import.
"""

import os
import sys
from pathlib import Path

# Set test env BEFORE any project import.
os.environ.setdefault("ACTIVE_PROVIDER", "miro")
os.environ.setdefault("MIRO_CLIENT_ID", "miro-test-id")
os.environ.setdefault("MIRO_CLIENT_SECRET", "miro-test-secret")
os.environ.setdefault("MIRO_REDIRECT_URI", "http://localhost:8080/auth/miro/callback")
os.environ.setdefault("FIGMA_CLIENT_ID", "figma-test-id")
os.environ.setdefault("FIGMA_CLIENT_SECRET", "figma-test-secret")
os.environ.setdefault("FIGMA_REDIRECT_URI", "http://localhost:8080/auth/figma/callback")
os.environ.setdefault("LUCID_CLIENT_ID", "lucid-test-id")
os.environ.setdefault("LUCID_CLIENT_SECRET", "lucid-test-secret")
os.environ.setdefault("LUCID_REDIRECT_URI", "http://localhost:8080/auth/lucid/callback")
os.environ.setdefault("ALLOWED_ORIGINS", "*")
os.environ.setdefault("PORT", "8080")

# Put repo root on sys.path so `import server`, `from config import config`, etc. work
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402


class _NoOpStore:
    """Stub for tests that still pass `temp_db` as a fixture. In passthrough
    mode the proxy never reads/writes a per-user token store, so save_tokens
    is a no-op. Existing tests' `await temp_db.save_tokens(...)` calls still
    work without modification."""

    async def save_tokens(self, *args, **kwargs):  # noqa: D401
        return True

    async def get_token_info(self, *args, **kwargs):
        return None


@pytest.fixture
def temp_db():
    """No-op placeholder for tests carried over from the persistent-store era."""
    return _NoOpStore()


@pytest.fixture
def reset_context_vars():
    """Clear request-scoped ContextVars between tests."""
    from config import config

    tokens = [
        config.current_user_email.set(""),
        config.current_host.set(""),
        config.current_saas_token.set(""),
        config.current_saas_scopes.set([]),
    ]
    yield
    # Reset in reverse order
    config.current_saas_scopes.reset(tokens[3])
    config.current_saas_token.reset(tokens[2])
    config.current_host.reset(tokens[1])
    config.current_user_email.reset(tokens[0])


@pytest.fixture
def set_user_context():
    """Helper to populate ContextVars for a test."""
    from config import config

    def _set(email="alice@example.com", host="localhost:8080", token="", scopes=None):
        config.current_user_email.set(email)
        config.current_host.set(host)
        config.current_saas_token.set(token)
        config.current_saas_scopes.set(scopes or [])

    return _set
