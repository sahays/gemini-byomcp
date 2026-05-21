"""
Shared pytest fixtures + environment bootstrap.

IMPORTANT: env vars are set at module import time so that project modules
(config.py, database.py, providers/*) pick them up on their first import.
"""

import os
import sys
from pathlib import Path

# Set test env BEFORE any project import.
os.environ.setdefault("ACTIVE_PROVIDER", "miro")
os.environ.setdefault("GOOGLE_CLIENT_ID", "1234-test.apps.googleusercontent.com")
os.environ.setdefault("MIRO_CLIENT_ID", "miro-test-id")
os.environ.setdefault("MIRO_CLIENT_SECRET", "miro-test-secret")
os.environ.setdefault("MIRO_REDIRECT_URI", "http://localhost:8080/auth/miro/callback")
os.environ.setdefault("FIGMA_CLIENT_ID", "figma-test-id")
os.environ.setdefault("FIGMA_CLIENT_SECRET", "figma-test-secret")
os.environ.setdefault("FIGMA_REDIRECT_URI", "http://localhost:8080/auth/figma/callback")
os.environ.setdefault("LUCID_CLIENT_ID", "lucid-test-id")
os.environ.setdefault("LUCID_CLIENT_SECRET", "lucid-test-secret")
os.environ.setdefault("LUCID_REDIRECT_URI", "http://localhost:8080/auth/lucid/callback")
# Force LocalJsonStore (Firestore disabled)
os.environ.setdefault("GCP_PROJECT_ID", "")
os.environ.setdefault("ALLOWED_ORIGINS", "*")
os.environ.setdefault("PORT", "8080")

# Put repo root on sys.path so `import server`, `import database`, etc. work
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Swap the module-level `db` with a per-test LocalJsonStore in tmp_path."""
    import database
    from database import LocalJsonStore

    store = LocalJsonStore(filepath=str(tmp_path / "default.json"))
    monkeypatch.setattr(database, "db", store)
    return store


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
