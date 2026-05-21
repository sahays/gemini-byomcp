"""
providers/base.py
Defines the plugin-based contract for all SaaS providers (Miro, Figma, Lucid).
Implements the unified scope validation decorator, refresh-token retry helper,
auth-boundary wrapper, and global registry pattern.
"""

import functools
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx
from fastmcp import FastMCP

from config import config

logger = logging.getLogger("mcp-providers")


class MissingScopeException(Exception):
    """Raised when the active corporate user lacks necessary permissions for a tool."""

    def __init__(self, provider: str, required_scopes: list[str], active_scopes: list[str]):
        self.provider = provider
        self.required_scopes = required_scopes
        self.active_scopes = active_scopes
        super().__init__(f"Provider {provider} is missing required scopes: {required_scopes}")


class RefreshFailedException(Exception):
    """Raised when a refresh-token exchange fails. Surfaces a re-auth link to the caller."""


def require_scopes(required_scopes: list[str]):
    """
    Decorator that asserts the active request context contains all required OAuth scopes.
    Raises MissingScopeException on failure; the auth-boundary wrapper translates that
    into a human-readable re-authorization prompt.
    """

    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            user_email = config.current_user_email.get()
            active_token = config.current_saas_token.get()

            if not active_token:
                logger.warning(f"Access Denied: No OAuth token found for {user_email}")
                raise MissingScopeException(config.ACTIVE_PROVIDER, required_scopes, [])

            active_scopes = config.current_saas_scopes.get()
            missing = [s for s in required_scopes if s not in active_scopes]
            if missing:
                logger.warning(
                    f"Access Denied: {user_email} missing scopes {missing}. "
                    f"Required: {required_scopes}, Active: {active_scopes}"
                )
                raise MissingScopeException(config.ACTIVE_PROVIDER, required_scopes, active_scopes)

            return await func(*args, **kwargs)

        return wrapper

    return decorator


def compute_expires_at(expires_in_seconds: int | None, safety_margin_s: int = 60) -> str:
    """Return ISO-8601 UTC timestamp at which the token should be considered expired."""
    if not expires_in_seconds:
        return ""
    expiry = datetime.now(tz=UTC) + timedelta(seconds=expires_in_seconds - safety_margin_s)
    return expiry.isoformat()


def is_expired(expires_at_iso: str) -> bool:
    """Return True if the ISO timestamp is in the past. Empty string => unknown, treat as not expired."""
    if not expires_at_iso:
        return False
    try:
        expiry = datetime.fromisoformat(expires_at_iso)
        return datetime.now(tz=UTC) >= expiry
    except ValueError:
        return False


def auth_boundary(provider_name: str):
    """
    Wrap a tool coroutine so MissingScopeException becomes a human-friendly auth link.
    Identical UX across all providers — link points at /auth/{provider_name}.
    """

    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapped(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except MissingScopeException as e:
                if not e.active_scopes:
                    user_email = config.current_user_email.get()
                    host = config.current_host.get()
                    protocol = "http" if "localhost" in host or "127.0.0.1" in host else "https"
                    auth_url = f"{protocol}://{host}/auth/{provider_name}?user={user_email}"
                    return (
                        f"[ACTION REQUIRED] Your {provider_name.capitalize()} account is not linked.\n"
                        f"Please link your account securely by visiting this authorization link:\n"
                        f"{auth_url}"
                    )
                missing = [s for s in e.required_scopes if s not in e.active_scopes]
                label = "scope" if len(missing) == 1 else "scopes"
                return (
                    f"[PERMISSION REQUIRED] {provider_name.capitalize()} {label} "
                    f"{', '.join(repr(s) for s in missing)} not granted."
                )
            except RefreshFailedException:
                user_email = config.current_user_email.get()
                host = config.current_host.get()
                protocol = "http" if "localhost" in host or "127.0.0.1" in host else "https"
                auth_url = f"{protocol}://{host}/auth/{provider_name}?user={user_email}"
                return (
                    f"[ACTION REQUIRED] Your {provider_name.capitalize()} session expired and "
                    f"could not be refreshed automatically.\n"
                    f"Please re-link your account: {auth_url}"
                )
            except Exception as e:
                logger.error(f"Unexpected tool error: {e}", exc_info=True)
                return f"Error: An unexpected internal error occurred during processing: {str(e)}"

        return wrapped

    return decorator


HttpCall = Callable[[str], Awaitable[httpx.Response]]


async def call_with_refresh(provider: "BaseProvider", request_fn: HttpCall) -> httpx.Response:
    """
    Execute request_fn(access_token). If the response is 401, refresh the user's token,
    persist the new tokens, update the active ContextVars, and retry once.

    request_fn must be a coroutine that accepts a bearer token string and returns an httpx.Response.
    """
    # Local import avoids a circular dependency between database and providers
    from database import db

    token = config.current_saas_token.get()
    response = await request_fn(token)
    if response.status_code != 401:
        return response

    user_email = config.current_user_email.get()
    token_info = await db.get_token_info(user_email, provider.name)
    refresh_token = (token_info or {}).get("refresh_token", "")
    if not refresh_token:
        logger.warning(f"401 from {provider.name} for {user_email}; no refresh_token available.")
        raise RefreshFailedException(f"No refresh_token stored for {provider.name}")

    try:
        new_tokens = await provider.refresh_access_token(refresh_token)
    except Exception as e:
        logger.error(f"Refresh failed for {provider.name}/{user_email}: {e}")
        raise RefreshFailedException(str(e)) from e

    new_access = new_tokens.get("access_token")
    if not new_access:
        raise RefreshFailedException(f"{provider.name} refresh returned empty access_token")

    # Some providers omit refresh_token in the refresh response; preserve the original.
    persisted_refresh = new_tokens.get("refresh_token") or refresh_token
    persisted_scopes = new_tokens.get("scopes") or (token_info or {}).get("scopes", [])

    await db.save_tokens(
        email=user_email,
        provider=provider.name,
        access_token=new_access,
        refresh_token=persisted_refresh,
        scopes=persisted_scopes,
        expires_at=new_tokens.get("expires_at", ""),
    )

    # Refresh the request-scoped token so downstream code sees the new value.
    config.current_saas_token.set(new_access)
    config.current_saas_scopes.set(persisted_scopes)

    logger.info(f"Refreshed {provider.name} token for {user_email}; retrying request.")
    return await request_fn(new_access)


class BaseProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        """The key name identifying this provider (e.g., 'miro', 'figma', 'lucid')."""

    @abstractmethod
    def get_auth_url(self, user_email: str) -> str:
        """Generate the OAuth consent URL for this provider."""

    @abstractmethod
    async def exchange_code_for_tokens(self, code: str, redirect_uri: str) -> dict:
        """Trade authorization code for {access_token, refresh_token, scopes, expires_at}."""

    @abstractmethod
    async def refresh_access_token(self, refresh_token: str) -> dict:
        """Trade a refresh_token for a new {access_token, refresh_token?, scopes?, expires_at}."""

    @abstractmethod
    def register_mcp_tools(self, mcp_app: FastMCP):
        """Bind tool mappings onto the FastMCP instance."""


class ProviderRegistry:
    """A plugin registry for dynamic SaaS integrations."""

    def __init__(self):
        self._providers: dict[str, BaseProvider] = {}

    def register(self, provider: BaseProvider):
        self._providers[provider.name] = provider
        logger.info(f"Registered SaaS Provider plugin: [{provider.name}]")

    def get(self, name: str) -> BaseProvider:
        if name not in self._providers:
            raise ValueError(f"SaaS Provider '{name}' is not supported or loaded.")
        return self._providers[name]

    def list_providers(self) -> list[str]:
        return list(self._providers.keys())


registry = ProviderRegistry()
