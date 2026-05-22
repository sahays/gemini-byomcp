"""
server.py — Option A (passthrough) variant.

Gemini Enterprise owns the OAuth flow with the SaaS (Miro/Figma/Lucid) directly.
GE sends the user's SaaS bearer token to /mcp in the Authorization header; this
proxy forwards it verbatim when calling the SaaS API. No Google tokeninfo
validation, no /auth/* routes, no Firestore token store, no refresh logic.

The branch is intended to be deployed side-by-side with the main branch so the
two architectures can be compared on the same GCP project.
"""

import sys

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastmcp import FastMCP

from config import config
from logging_setup import (
    Timer,
    configure_logging,
    current_request_id,
    log_event,
    new_request_id,
)
from providers import registry

configure_logging()

active_provider_name = config.ACTIVE_PROVIDER
log_event(
    "info",
    "boot.init_fastmcp",
    logger_name="mcp-gateway",
    provider=active_provider_name,
    mode="passthrough",
)

_mcp = FastMCP(
    name=f"Gemini-Enterprise-{active_provider_name.capitalize()}-Gateway",
)

try:
    active_provider = registry.get(active_provider_name)
    active_provider.register_mcp_tools(_mcp)
except Exception as e:
    log_event(
        "critical",
        "boot.provider_load_failed",
        logger_name="mcp-gateway",
        provider=active_provider_name,
        error=str(e),
    )
    sys.exit(1)

# Discover the provider's full scope list once; in passthrough mode we assume
# the token GE sends carries whatever scopes were granted at consent time, so
# the @require_scopes pre-check should always pass. Miro/Figma/Lucid will
# enforce the actual scopes via 403 if the token is under-privileged.
try:
    from providers.miro import ALL_SCOPES as _MIRO_SCOPES
except ImportError:
    _MIRO_SCOPES = []
try:
    from providers.figma import ALL_SCOPES as _FIGMA_SCOPES
except ImportError:
    _FIGMA_SCOPES = []
try:
    from providers.lucid import ALL_SCOPES as _LUCID_SCOPES
except ImportError:
    _LUCID_SCOPES = []

_PROVIDER_SCOPES = {
    "miro": _MIRO_SCOPES,
    "figma": _FIGMA_SCOPES,
    "lucid": _LUCID_SCOPES,
}
ACTIVE_PROVIDER_SCOPES = _PROVIDER_SCOPES.get(active_provider_name, [])

# Mount FastMCP at exactly /mcp (no trailing slash) so we don't issue 307s
# to clients (like GE) that don't follow redirects on POST.
mcp_asgi_app = _mcp.http_app(path="/mcp", transport="streamable-http")

app = FastAPI(
    title="Gemini Enterprise Custom MCP Gateway (passthrough)",
    description="Option A: stateless SaaS-token passthrough; GE owns OAuth.",
    lifespan=mcp_asgi_app.lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Passthrough Bearer Middleware ---
@app.middleware("http")
async def bearer_passthrough_middleware(request: Request, call_next):
    # Assign or honor inbound request id; propagated to every log line in this scope.
    rid = request.headers.get("X-Request-Id") or new_request_id()
    ctx_rid = current_request_id.set(rid)

    # /health and /  are public.
    if request.url.path in ["/health", "/"]:
        try:
            return await call_next(request)
        finally:
            current_request_id.reset(ctx_rid)

    log_event(
        "info",
        "http.ingress",
        logger_name="mcp-gateway",
        method=request.method,
        path=request.url.path,
        origin=request.headers.get("origin", ""),
        user_agent=request.headers.get("user-agent", "")[:120],
    )

    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        log_event(
            "warning",
            "http.unauthenticated",
            logger_name="mcp-gateway",
            path=request.url.path,
        )
        current_request_id.reset(ctx_rid)
        return JSONResponse(
            status_code=401,
            content={
                "error": "Missing Bearer token. Gemini Enterprise must send the SaaS access token."
            },
            headers={"WWW-Authenticate": 'Bearer realm="mcp"'},
        )

    token = auth_header.split(" ", 1)[1]

    # Populate request-scoped ContextVars so the existing provider tools work
    # unchanged. We don't know the granted scopes here (the SaaS token is
    # opaque), so we assume the full ALL_SCOPES set — the SaaS itself will
    # enforce on the actual call and return 403 if a scope is missing.
    ctx_token = config.current_saas_token.set(token)
    ctx_scopes = config.current_saas_scopes.set(list(ACTIVE_PROVIDER_SCOPES))
    ctx_host = config.current_host.set(request.headers.get("host", "localhost:8080"))
    ctx_email = config.current_user_email.set("ge-passthrough")

    try:
        with Timer() as t:
            response = await call_next(request)
        log_event(
            "info",
            "http.response",
            logger_name="mcp-gateway",
            path=request.url.path,
            status_code=response.status_code,
            latency_ms=t.elapsed_ms,
        )
        return response
    finally:
        config.current_saas_token.reset(ctx_token)
        config.current_saas_scopes.reset(ctx_scopes)
        config.current_host.reset(ctx_host)
        config.current_user_email.reset(ctx_email)
        current_request_id.reset(ctx_rid)


@app.get("/health")
def health():
    return {
        "status": "healthy",
        "service": f"mcp-gateway-{config.ACTIVE_PROVIDER}",
        "active_provider": config.ACTIVE_PROVIDER,
        "registry": registry.list_providers(),
        "mode": "passthrough",
    }


# --- FastMCP App Mount ---
app.mount("/", mcp_asgi_app)

if __name__ == "__main__":
    import uvicorn

    log_event(
        "info",
        "boot.serving",
        logger_name="mcp-gateway",
        port=config.PORT,
        provider=active_provider_name,
    )
    uvicorn.run("server:app", host="0.0.0.0", port=config.PORT, log_level="warning")
