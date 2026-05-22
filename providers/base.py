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
from logging_setup import Timer, log_event

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
                log_event(
                    "warning",
                    "scope.no_token",
                    logger_name="mcp-providers",
                    tool=func.__name__,
                    provider=config.ACTIVE_PROVIDER,
                    user_email=user_email,
                )
                raise MissingScopeException(config.ACTIVE_PROVIDER, required_scopes, [])

            active_scopes = config.current_saas_scopes.get()
            missing = [s for s in required_scopes if s not in active_scopes]
            if missing:
                log_event(
                    "warning",
                    "scope.denied",
                    logger_name="mcp-providers",
                    tool=func.__name__,
                    provider=config.ACTIVE_PROVIDER,
                    required_scopes=required_scopes,
                    missing_scopes=missing,
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
    Passthrough-mode variant: Gemini Enterprise owns the OAuth flow, so the
    proxy never produces re-link URLs of its own. When the bearer token is
    missing, missing a scope, or rejected by the SaaS, we surface a clear
    message — GE will detect a 401 from /mcp (or the [ACTION REQUIRED] text
    in a tool reply) and re-OAuth the user on its side.
    """

    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapped(*args, **kwargs):
            log_event(
                "info",
                "tool.invoke",
                logger_name="mcp-providers",
                tool=func.__name__,
                provider=provider_name,
                kwargs=_redact_kwargs(kwargs),
            )
            try:
                with Timer() as t:
                    result = await func(*args, **kwargs)
                log_event(
                    "info",
                    "tool.success",
                    logger_name="mcp-providers",
                    tool=func.__name__,
                    provider=provider_name,
                    latency_ms=t.elapsed_ms,
                )
                return result
            except MissingScopeException as e:
                if not e.active_scopes:
                    return (
                        f"[ACTION REQUIRED] No {provider_name.capitalize()} bearer token "
                        f"was received. Have Gemini Enterprise re-authenticate the user."
                    )
                missing = [s for s in e.required_scopes if s not in e.active_scopes]
                label = "scope" if len(missing) == 1 else "scopes"
                return (
                    f"[PERMISSION REQUIRED] {provider_name.capitalize()} {label} "
                    f"{', '.join(repr(s) for s in missing)} not granted in the data-source "
                    f"OAuth config. Add it in the Gemini Enterprise Authentication settings."
                )
            except RefreshFailedException:
                return (
                    f"[ACTION REQUIRED] The {provider_name.capitalize()} bearer token was "
                    f"rejected (expired or invalid). Gemini Enterprise should re-OAuth the "
                    f"user against the SaaS."
                )
            except Exception as e:
                log_event(
                    "error",
                    "tool.error",
                    logger_name="mcp-providers",
                    tool=func.__name__,
                    provider=provider_name,
                    error=str(e),
                )
                logger.error(f"Unexpected tool error: {e}", exc_info=True)
                return f"Error: An unexpected internal error occurred during processing: {str(e)}"

        return wrapped

    return decorator


_REDACT_KEYS = {"token", "access_token", "refresh_token", "secret", "client_secret"}


def _redact_kwargs(kwargs: dict) -> dict:
    """Strip obvious secret-looking keys before logging tool kwargs."""
    return {k: ("<redacted>" if k.lower() in _REDACT_KEYS else v) for k, v in kwargs.items()}


HttpCall = Callable[[str], Awaitable[httpx.Response]]


async def call_with_refresh(provider: "BaseProvider", request_fn: HttpCall) -> httpx.Response:
    """
    Passthrough-mode variant: the proxy doesn't hold a refresh_token (Gemini
    Enterprise owns OAuth in this branch), so we never attempt to refresh.
    A 401 from the SaaS means the bearer token GE sent is invalid/expired —
    we surface that to the caller via RefreshFailedException so auth_boundary
    can return a re-auth-needed message; GE will then redo the OAuth flow with
    the user on its side.
    """
    token = config.current_saas_token.get()
    with Timer() as t:
        response = await request_fn(token)
    try:
        req = response.request
        method = req.method
        url = str(req.url)
    except RuntimeError:
        method = "?"
        url = "?"
    log_event(
        "info" if response.status_code < 400 else "warning",
        "saas.call",
        logger_name="mcp-providers",
        provider=provider.name,
        method=method,
        url=url,
        status_code=response.status_code,
        latency_ms=t.elapsed_ms,
    )
    if response.status_code != 401:
        return response

    log_event(
        "warning",
        "saas.401_reauth_needed",
        logger_name="mcp-providers",
        provider=provider.name,
    )
    raise RefreshFailedException(
        f"{provider.name} rejected the bearer token; GE needs to re-authenticate the user."
    )


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
