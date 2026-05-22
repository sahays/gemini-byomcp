"""
config.py
Centralized configuration management for the extensible custom MCP proxy.
Uses thread-safe ContextVars to isolate users in a concurrent async environment.
"""

import os
from contextvars import ContextVar

from dotenv import load_dotenv

# Load local .env file if it exists
load_dotenv()


class Config:
    # --- Server settings ---
    PORT = int(os.environ.get("PORT", 8080))
    ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")

    # Determines which SaaS provider this specific deployment runs (e.g. 'miro', 'figma', 'lucid')
    ACTIVE_PROVIDER = os.environ.get("ACTIVE_PROVIDER", "miro").lower()

    # --- Inbound identity (Google WIF) ---
    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")

    # --- Outbound identity (SaaS settings) ---
    MIRO_CLIENT_ID = os.environ.get("MIRO_CLIENT_ID", "")
    MIRO_CLIENT_SECRET = os.environ.get("MIRO_CLIENT_SECRET", "")
    MIRO_REDIRECT_URI = os.environ.get(
        "MIRO_REDIRECT_URI", "http://localhost:8080/auth/miro/callback"
    )

    FIGMA_CLIENT_ID = os.environ.get("FIGMA_CLIENT_ID", "")
    FIGMA_CLIENT_SECRET = os.environ.get("FIGMA_CLIENT_SECRET", "")
    FIGMA_REDIRECT_URI = os.environ.get(
        "FIGMA_REDIRECT_URI", "http://localhost:8080/auth/figma/callback"
    )

    LUCID_CLIENT_ID = os.environ.get("LUCID_CLIENT_ID", "")
    LUCID_CLIENT_SECRET = os.environ.get("LUCID_CLIENT_SECRET", "")
    LUCID_REDIRECT_URI = os.environ.get(
        "LUCID_REDIRECT_URI", "http://localhost:8080/auth/lucid/callback"
    )

    # --- Request-Scoped Context Variables ---
    # Thread-safe context variables populated by the identity middleware
    current_user_email: ContextVar[str] = ContextVar("current_user_email", default="")
    current_host: ContextVar[str] = ContextVar("current_host", default="")
    current_saas_token: ContextVar[str] = ContextVar("current_saas_token", default="")
    # Default is a fresh empty list per process; the middleware always .set()s a new list
    # per request, so the default is never mutated. Suppressing B039 deliberately.
    current_saas_scopes: ContextVar[list[str]] = ContextVar("current_saas_scopes", default=[])  # noqa: B039


# Instantiate for easy importing
config = Config()
