"""
server.py — Option A (passthrough) variant.

Gemini Enterprise owns the OAuth flow with the SaaS (Miro/Figma/Lucid) directly.
GE sends the user's SaaS bearer token to /mcp in the Authorization header; this
proxy forwards it verbatim when calling the SaaS API. No Google tokeninfo
validation, no /auth/* routes, no Firestore token store, no refresh logic.

The branch is intended to be deployed side-by-side with the main branch so the
two architectures can be compared on the same GCP project.
"""

import logging
import sys

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastmcp import FastMCP

from config import config
from providers import registry

# --- Structured Semantic Logger Config ---
logging.basicConfig(
    level=logging.INFO,
    format='{"timestamp":"%(asctime)s", "severity":"%(levelname)s", "logger":"%(name)s", "message":"%(message)s"}',
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("mcp-gateway")

# 1. Initialize FastMCP FIRST based on the active deployment provider
active_provider_name = config.ACTIVE_PROVIDER
logger.info(
    f"Initializing FastMCP for active provider: [{active_provider_name}] (passthrough mode)"
)

_mcp = FastMCP(
    name=f"Gemini-Enterprise-{active_provider_name.capitalize()}-Gateway",
)

# 2. Register tools for the active provider
try:
    active_provider = registry.get(active_provider_name)
    active_provider.register_mcp_tools(_mcp)
except Exception as e:
    logger.critical(f"Failed to load active provider tools: {e}")
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

mcp_asgi_app = _mcp.http_app(path="/", transport="streamable-http")

# 3. Initialize FastAPI App
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
    # /health is public; everything else needs a bearer token.
    if request.url.path in ["/health", "/"]:
        return await call_next(request)

    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        logger.warning("Unauthenticated request blocked: missing Bearer token.")
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
    # No identity available in passthrough mode; tools that reference
    # current_user_email use it only for re-link prompts which don't apply here.
    ctx_email = config.current_user_email.set("ge-passthrough")

    try:
        return await call_next(request)
    finally:
        config.current_saas_token.reset(ctx_token)
        config.current_saas_scopes.reset(ctx_scopes)
        config.current_host.reset(ctx_host)
        config.current_user_email.reset(ctx_email)


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
app.mount("/mcp", mcp_asgi_app)

if __name__ == "__main__":
    import uvicorn

    logger.info(f"Launching passthrough MCP Gateway on port {config.PORT}")
    uvicorn.run("server:app", host="0.0.0.0", port=config.PORT, log_level="warning")
