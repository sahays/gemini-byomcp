"""
server.py
Core production gateway server.
Implements inbound Google bearer-token validation, request-scoped identity isolation,
pre-emptive SaaS token refresh, and dynamic SaaS routing via the provider registry.
"""

import logging
import sys

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastmcp import FastMCP

from config import config
from database import db
from providers import registry
from providers.base import is_expired

# --- Structured Semantic Logger Config ---
logging.basicConfig(
    level=logging.INFO,
    format='{"timestamp":"%(asctime)s", "severity":"%(levelname)s", "logger":"%(name)s", "message":"%(message)s"}',
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("mcp-gateway")

# 1. Initialize FastMCP FIRST based on the active deployment provider
active_provider_name = config.ACTIVE_PROVIDER
logger.info(f"Initializing FastMCP for active provider: [{active_provider_name}]")

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

mcp_asgi_app = _mcp.http_app(path="/", transport="streamable-http")

# 3. Initialize FastAPI App
app = FastAPI(
    title="Gemini Enterprise Custom MCP Gateway",
    description="An enterprise-grade, DRY custom MCP gateway proxy.",
    lifespan=mcp_asgi_app.lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- 1. Inbound Identity Middleware (validates Gemini Enterprise's bearer token) ---
@app.middleware("http")
async def wif_identity_vault_middleware(request: Request, call_next):
    # Public routes do not require inbound authentication
    if request.url.path.startswith("/auth/") or request.url.path in ["/health", "/"]:
        return await call_next(request)

    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        logger.warning("Unauthenticated request blocked: Missing Bearer token.")
        return JSONResponse(status_code=401, content={"error": "Missing WIF Bearer token."})

    token = auth_header.split(" ")[1]

    try:
        # Validate Google-issued access token via tokeninfo
        async with httpx.AsyncClient() as client:
            token_response = await client.get(
                f"https://oauth2.googleapis.com/tokeninfo?access_token={token}"
            )
            if token_response.status_code != 200:
                logger.warning(
                    f"Unauthenticated request blocked: Invalid Google Token. "
                    f"Response: {token_response.text}"
                )
                return JSONResponse(
                    status_code=401, content={"error": "Invalid or expired Google Token."}
                )
            decoded_token = token_response.json()

        # Validate Audience / Authorized Party
        token_aud = decoded_token.get("aud")
        token_azp = decoded_token.get("azp")
        if config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_ID not in [token_aud, token_azp]:
            logger.error("Security Breach Blocked: Token Audience or Client ID mismatch.")
            return JSONResponse(
                status_code=401, content={"error": "Token Audience/Authorized Party mismatch."}
            )

        user_email = decoded_token.get("email")
        if not user_email:
            logger.error("Forbidden request blocked: Google token missing user email claim.")
            return JSONResponse(
                status_code=403, content={"error": "Token missing mapped email claim."}
            )

        # Fetch the SaaS token, and pre-emptively refresh if expired.
        token_info = await db.get_token_info(user_email, config.ACTIVE_PROVIDER)
        if (
            token_info
            and token_info.get("refresh_token")
            and is_expired(token_info.get("expires_at", ""))
        ):
            try:
                refreshed = await active_provider.refresh_access_token(token_info["refresh_token"])
                persisted_refresh = refreshed.get("refresh_token") or token_info["refresh_token"]
                persisted_scopes = refreshed.get("scopes") or token_info.get("scopes", [])
                await db.save_tokens(
                    email=user_email,
                    provider=config.ACTIVE_PROVIDER,
                    access_token=refreshed["access_token"],
                    refresh_token=persisted_refresh,
                    scopes=persisted_scopes,
                    expires_at=refreshed.get("expires_at", ""),
                )
                token_info = {
                    "access_token": refreshed["access_token"],
                    "refresh_token": persisted_refresh,
                    "scopes": persisted_scopes,
                    "expires_at": refreshed.get("expires_at", ""),
                }
                logger.info(
                    f"Pre-emptively refreshed {config.ACTIVE_PROVIDER} token for {user_email}"
                )
            except Exception as refresh_err:
                logger.warning(
                    f"Pre-emptive refresh failed for {user_email}/{config.ACTIVE_PROVIDER}: "
                    f"{refresh_err}. Letting call_with_refresh handle on next 401."
                )

        saas_token = token_info.get("access_token", "") if token_info else ""
        saas_scopes = token_info.get("scopes", []) if token_info else []

        ctx_email = config.current_user_email.set(user_email)
        ctx_host = config.current_host.set(request.headers.get("host", "localhost:8080"))
        ctx_token = config.current_saas_token.set(saas_token)
        ctx_scopes = config.current_saas_scopes.set(saas_scopes)

        try:
            logger.info(
                f"Verified inbound corporate request for [{user_email}] on provider "
                f"[{config.ACTIVE_PROVIDER}]"
            )
            return await call_next(request)
        finally:
            config.current_user_email.reset(ctx_email)
            config.current_host.reset(ctx_host)
            config.current_saas_token.reset(ctx_token)
            config.current_saas_scopes.reset(ctx_scopes)

    except Exception as e:
        logger.error(f"Internal Identity Error: {str(e)}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"error": "An internal identity processing error occurred."}
        )


# --- 2. Dynamic OAuth 2.0 Web Routes ---
@app.get("/auth/{provider_name}")
async def authorize_saas(provider_name: str, user: str):
    """Step 1: Generic route to initiate the OAuth flow for any registered SaaS."""
    try:
        provider = registry.get(provider_name)
        if not user:
            raise HTTPException(
                status_code=400, detail="User corporate email parameter is required."
            )
        logger.info(f"Initiating OAuth Consent for user [{user}] on SaaS [{provider_name}]")
        return RedirectResponse(url=provider.get_auth_url(user))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@app.get("/auth/{provider_name}/callback")
async def saas_oauth_callback(provider_name: str, code: str, state: str, request: Request):
    """Step 2 & 3: Generic OAuth callback. Exchanges codes and persists tokens."""
    try:
        provider = registry.get(provider_name)
        user_email = state  # the corporate email we sent through `state`

        host = request.headers.get("host", "localhost:8080")
        protocol = "http" if "localhost" in host or "127.0.0.1" in host else "https"
        redirect_uri = f"{protocol}://{host}/auth/{provider_name}/callback"

        logger.info(f"Exchanging temporary authorization code for SaaS [{provider_name}] callback")
        token_data = await provider.exchange_code_for_tokens(code, redirect_uri)

        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token", "")
        scopes = token_data.get("scopes", [])
        expires_at = token_data.get("expires_at", "")

        if not access_token:
            raise HTTPException(
                status_code=500,
                detail=f"SaaS provider {provider_name} returned an empty access token payload.",
            )

        success = await db.save_tokens(
            email=user_email,
            provider=provider_name,
            access_token=access_token,
            refresh_token=refresh_token,
            scopes=scopes,
            expires_at=expires_at,
        )

        if success:
            logger.info(
                f"Successfully linked SaaS [{provider_name}] credentials for corporate user [{user_email}]"
            )
            return HTMLResponse(
                content=f"""
                <div style="font-family: Arial, sans-serif; text-align: center; margin-top: 10%;">
                    <h2 style="color: #2e7d32;">Authentication Successful!</h2>
                    <p style="font-size: 16px;">Your {provider_name.capitalize()} account is now securely linked.</p>
                    <p>You can close this window and return to Gemini Enterprise.</p>
                </div>
                """,
                status_code=200,
            )
        raise HTTPException(
            status_code=500, detail="Database write failure occurred while storing tokens."
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Callback processing failed for SaaS [{provider_name}]: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"OAuth Handshake Error: {str(e)}") from e


@app.get("/health")
def health():
    return {
        "status": "healthy",
        "service": f"mcp-gateway-{config.ACTIVE_PROVIDER}",
        "active_provider": config.ACTIVE_PROVIDER,
        "registry": registry.list_providers(),
    }


# --- 3. FastMCP App Mount ---
app.mount("/mcp", mcp_asgi_app)

if __name__ == "__main__":
    import uvicorn

    logger.info(f"Launching production Custom MCP Gateway on port {config.PORT}")
    uvicorn.run("server:app", host="0.0.0.0", port=config.PORT, log_level="warning")
