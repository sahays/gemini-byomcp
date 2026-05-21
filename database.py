"""
database.py
Secure, asynchronous token storage layer.
Automatically uses GCP Firestore in production (if GCP_PROJECT_ID is set),
otherwise falls back to a thread-safe local JSON store (default.json) in development.
"""

import asyncio
import json
import logging
import os

from config import config

logger = logging.getLogger("mcp-database")


class LocalJsonStore:
    """A secure-by-locking JSON file store for sandbox/local development."""

    def __init__(self, filepath: str = "default.json"):
        self.filepath = filepath
        self.lock = asyncio.Lock()
        self._ensure_db_exists()

    def _ensure_db_exists(self):
        if not os.path.exists(self.filepath):
            with open(self.filepath, "w") as f:
                json.dump({}, f)
            logger.info(f"Initialized sandbox local JSON store at {self.filepath}")

    def _load(self) -> dict:
        try:
            with open(self.filepath) as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return {}

    def _save(self, data: dict):
        with open(self.filepath, "w") as f:
            json.dump(data, f, indent=4)

    async def save_tokens(
        self,
        email: str,
        provider: str,
        access_token: str,
        refresh_token: str,
        scopes: list[str],
        expires_at: str | None = None,
    ) -> bool:
        try:
            async with self.lock:
                data = self._load()
                if email not in data:
                    data[email] = {}

                data[email][f"{provider}_access_token"] = access_token
                data[email][f"{provider}_refresh_token"] = refresh_token
                data[email][f"{provider}_scopes"] = scopes
                data[email][f"{provider}_expires_at"] = expires_at or ""

                self._save(data)
                logger.info(f"LocalJSON: Saved {provider} tokens and scopes for {email}")
                return True
        except Exception as e:
            logger.error(f"LocalJSON Save Error: {e}")
            return False

    async def get_token_info(self, email: str, provider: str) -> dict | None:
        try:
            async with self.lock:
                data = self._load()
            user_data = data.get(email, {})
            access_token = user_data.get(f"{provider}_access_token")
            scopes = user_data.get(f"{provider}_scopes", [])

            if access_token:
                return {
                    "access_token": access_token,
                    "refresh_token": user_data.get(f"{provider}_refresh_token", ""),
                    "scopes": scopes,
                    "expires_at": user_data.get(f"{provider}_expires_at", ""),
                }
            return None
        except Exception as e:
            logger.error(f"LocalJSON Fetch Error: {e}")
            return None


class FirestoreStore:
    """Production GCP Firestore store utilizing enterprise collections."""

    def __init__(self, project_id: str):
        from google.cloud import firestore

        self.db = firestore.AsyncClient(project=project_id)
        logger.info(f"Initialized Firestore Client under GCP Project: {project_id}")

    async def save_tokens(
        self,
        email: str,
        provider: str,
        access_token: str,
        refresh_token: str,
        scopes: list[str],
        expires_at: str | None = None,
    ) -> bool:
        try:
            doc_ref = self.db.collection("mcp_users").document(email)
            await doc_ref.set(
                {
                    f"{provider}_access_token": access_token,
                    f"{provider}_refresh_token": refresh_token,
                    f"{provider}_scopes": scopes,
                    f"{provider}_expires_at": expires_at or "",
                },
                merge=True,
            )
            logger.info(f"Firestore: Saved {provider} tokens and scopes for {email}")
            return True
        except Exception as e:
            logger.error(f"Firestore Save Error for user {email}: {e}")
            return False

    async def get_token_info(self, email: str, provider: str) -> dict | None:
        try:
            doc_ref = self.db.collection("mcp_users").document(email)
            doc = await doc_ref.get()
            if doc.exists:
                data = doc.to_dict() or {}
                access_token = data.get(f"{provider}_access_token")
                scopes = data.get(f"{provider}_scopes", [])
                if access_token:
                    return {
                        "access_token": access_token,
                        "refresh_token": data.get(f"{provider}_refresh_token", ""),
                        "scopes": scopes,
                        "expires_at": data.get(f"{provider}_expires_at", ""),
                    }
            return None
        except Exception as e:
            logger.error(f"Firestore Fetch Error for user {email}: {e}")
            return None


# Auto-resolve the database backend based on configuration
if config.GCP_PROJECT_ID:
    try:
        db = FirestoreStore(project_id=config.GCP_PROJECT_ID)
    except ImportError:
        logger.warning("google-cloud-firestore not installed. Falling back to local JSON store.")
        db = LocalJsonStore()
else:
    db = LocalJsonStore()
