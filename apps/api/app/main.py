from contextlib import asynccontextmanager

from app.api.mcp_oauth import router as mcp_oauth_router
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.exceptions import AppError
from app.mcp.gate import McpAuthGate
from app.mcp.server import mcp, mcp_asgi_app
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from packages.database.session import init_engine

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_engine(settings.database_url, echo=settings.debug)
    # A mounted sub-application's own lifespan never runs, so the MCP
    # session manager has to be entered here instead (see app/mcp/server.py).
    async with mcp.session_manager.run():
        yield


# Interactive docs and the schema map every endpoint for an attacker, so they
# are only served in local environments (or when ENABLE_API_DOCS=true).
app = FastAPI(
    title="Call Center Platform API",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url="/redoc" if settings.docs_enabled else None,
    openapi_url="/openapi.json" if settings.docs_enabled else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "X-Requested-With"],
    max_age=600,
)

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "cross-origin",  # the webchat widget is embedded cross-site
    # This service returns JSON/TwiML/JS only; never render it as a page.
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}
_DOCS_PATHS = ("/docs", "/redoc")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for name, value in _SECURITY_HEADERS.items():
        # Swagger/ReDoc need inline scripts and a CDN, so skip the strict CSP there.
        if name == "Content-Security-Policy" and request.url.path.startswith(_DOCS_PATHS):
            continue
        response.headers.setdefault(name, value)
    if not settings.is_local:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
        )
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.get("/healthz", tags=["meta"])
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(api_router)
app.include_router(mcp_oauth_router)
# External MCP clients (ChatGPT, Claude, any other MCP host) connect here,
# authenticated with a per-tenant API key (POST /api/v1/mcp/api-keys) rather
# than a user JWT — see app/mcp/server.py and app/mcp/auth.py.
app.mount("/mcp", McpAuthGate(mcp_asgi_app))
