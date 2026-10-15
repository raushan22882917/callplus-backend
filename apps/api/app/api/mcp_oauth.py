"""OAuth 2.1 authorization server for the MCP endpoint, so OAuth-only AI hosts
can connect: RFC 9728 protected-resource metadata, RFC 8414 server metadata,
RFC 7591 dynamic client registration, authorization-code + PKCE (S256 only),
refresh-token rotation, RFC 7009 revocation, RFC 8707 resource binding.

Mounted at the application root (not /api/v1) because discovery lives under
/.well-known. Clients are public (no secret). The resource owner signs in
with their normal platform credentials (+2FA if enabled) on the authorize
page and picks which scopes to grant; they need the `mcp.manage` permission.
"""

import html
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from app.core.api_keys import SCOPE_PERMISSIONS
from app.core.audit import log_audit_event
from app.core.deps import get_db
from app.core.mcp_oauth import (
    ACCESS_PREFIX,
    ACCESS_TOKEN_SECONDS,
    CODE_SECONDS,
    DEFAULT_SCOPES,
    OAUTH_SCOPES,
    REFRESH_PREFIX,
    REFRESH_TOKEN_DAYS,
    hash_token,
    issuer,
    new_secret,
    pkce_matches,
    redirect_matches,
    resource_url,
    same_resource,
    valid_redirect_uri,
)
from app.core.permissions import Permissions
from app.core.rate_limit_ip import ip_rate_limit
from app.core.security import burn_password_check, verify_password
from app.services.platform_totp import get_enabled_totp, verify_second_factor
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from packages.database.models import (
    McpOAuthClient,
    McpOAuthCode,
    McpOAuthToken,
    MembershipStatus,
    Role,
    Tenant,
    TenantStatus,
    TenantUser,
    User,
)

router = APIRouter(tags=["mcp-oauth"])

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_PAGE_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"
)


def _json(body: dict, status_code: int = 200, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(body, status_code=status_code, headers={**_NO_STORE, **(headers or {})})


def _oauth_error(error: str, description: str, status_code: int = 400) -> JSONResponse:
    return _json({"error": error, "error_description": description}, status_code)


# ---------------------------------------------------------------- discovery


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
async def protected_resource_metadata() -> JSONResponse:
    return JSONResponse(
        {
            "resource": resource_url(),
            "authorization_servers": [issuer()],
            "scopes_supported": list(OAUTH_SCOPES),
            "bearer_methods_supported": ["header"],
            "resource_name": "AutoNxt Call Center MCP",
        },
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.get("/.well-known/oauth-authorization-server")
@router.get("/.well-known/oauth-authorization-server/mcp")
async def authorization_server_metadata() -> JSONResponse:
    base = issuer()
    return JSONResponse(
        {
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register",
            "revocation_endpoint": f"{base}/oauth/revoke",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": list(OAUTH_SCOPES),
            "authorization_response_iss_parameter_supported": True,
        },
        headers={"Cache-Control": "public, max-age=300"},
    )


# ---------------------------------------------------------------- registration


@router.post("/oauth/register", dependencies=[Depends(ip_rate_limit("oauth-register", limit=10))])
async def register_client(request: Request, db: AsyncSession = Depends(get_db)) -> JSONResponse:
    try:
        body = await request.json()
    except ValueError:
        return _oauth_error("invalid_client_metadata", "Body must be JSON")
    if not isinstance(body, dict):
        return _oauth_error("invalid_client_metadata", "Body must be a JSON object")
    uris = body.get("redirect_uris")
    if not isinstance(uris, list) or not 1 <= len(uris) <= 10:
        return _oauth_error("invalid_redirect_uri", "Provide 1-10 redirect_uris")
    if not all(valid_redirect_uri(u) for u in uris):
        return _oauth_error(
            "invalid_redirect_uri", "redirect_uris must be https, loopback http, or an app scheme"
        )
    method = body.get("token_endpoint_auth_method", "none")
    if method != "none":
        return _oauth_error("invalid_client_metadata", "Only public clients (auth method none)")
    name = str(body.get("client_name") or "MCP client").strip()[:200] or "MCP client"
    client = McpOAuthClient(client_name=name, redirect_uris=list(dict.fromkeys(uris)))
    db.add(client)
    await db.commit()
    return _json(
        {
            "client_id": str(client.id),
            "client_id_issued_at": int(datetime.now(UTC).timestamp()),
            "client_name": client.client_name,
            "redirect_uris": client.redirect_uris,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
        201,
    )


# ---------------------------------------------------------------- authorize


def _page(body: str, status_code: int = 200) -> HTMLResponse:
    doc = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Authorize access</title><style>"
        "body{font:16px system-ui,sans-serif;max-width:26rem;margin:3rem auto;padding:0 1rem;color:#111}"
        "label{display:block;margin:.6rem 0 .2rem;font-weight:600}"
        "input[type=text],input[type=email],input[type=password]{width:100%;padding:.5rem;box-sizing:border-box}"
        ".scope{font-weight:400;margin:.2rem 0}button{margin:1rem .5rem 0 0;padding:.6rem 1.1rem;font-size:1rem}"
        ".err{background:#fde8e8;padding:.6rem;border-radius:6px}code{background:#f2f2f2;padding:0 .2rem}"
        f"</style></head><body>{body}</body></html>"
    )
    return HTMLResponse(doc, status_code=status_code, headers={**_NO_STORE, "Content-Security-Policy": _PAGE_CSP})


def _error_page(message: str) -> HTMLResponse:
    return _page(f"<h1>Cannot continue</h1><p class='err'>{html.escape(message)}</p>", 400)


def _redirect(redirect_uri: str, params: dict[str, str]) -> RedirectResponse:
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(
        f"{redirect_uri}{sep}{urlencode({**params, 'iss': issuer()})}",
        status_code=303,
        headers=_NO_STORE,
    )


class _AuthRequest:
    """The validated authorization-request parameters."""

    def __init__(self, client: McpOAuthClient, params: dict[str, str]):
        self.client = client
        self.redirect_uri = params["redirect_uri"]
        self.state = params.get("state", "")
        self.challenge = params.get("code_challenge", "")
        self.resource = params.get("resource") or None
        self.requested = [s for s in params.get("scope", "").split() if s in OAUTH_SCOPES]

    def hidden_fields(self, params: dict[str, str]) -> str:
        keys = ("client_id", "redirect_uri", "state", "code_challenge", "code_challenge_method", "scope", "resource", "response_type")
        return "".join(
            f"<input type='hidden' name='{k}' value='{html.escape(params.get(k, ''), quote=True)}'>"
            for k in keys
        )


async def _validate_authorize(
    db: AsyncSession, params: dict[str, str]
) -> tuple[_AuthRequest | None, HTMLResponse | RedirectResponse | None]:
    """Never redirect for a bad client_id/redirect_uri (open-redirect guard);
    other problems go back to the client as an error redirect."""
    try:
        client = await db.get(McpOAuthClient, uuid.UUID(params.get("client_id", "")))
    except ValueError:
        client = None
    redirect_uri = params.get("redirect_uri", "")
    if client is None or not redirect_matches(client.redirect_uris, redirect_uri):
        return None, _error_page("Unknown application or redirect address.")
    state = {"state": params["state"]} if params.get("state") else {}
    if params.get("response_type") != "code":
        return None, _redirect(redirect_uri, {"error": "unsupported_response_type", **state})
    if params.get("code_challenge_method") != "S256" or not (43 <= len(params.get("code_challenge", "")) <= 128):
        return None, _redirect(
            redirect_uri,
            {"error": "invalid_request", "error_description": "PKCE S256 code_challenge required", **state},
        )
    if not same_resource(params.get("resource") or None):
        return None, _redirect(redirect_uri, {"error": "invalid_target", **state})
    return _AuthRequest(client, params), None


def _form(req: _AuthRequest, params: dict[str, str], error: str | None = None, email: str = "", slug: str = "") -> HTMLResponse:
    chosen = set(req.requested) if req.requested else set(DEFAULT_SCOPES)
    boxes = "".join(
        f"<div class='scope'><label class='scope'><input type='checkbox' name='grant' value='{s}'"
        f"{' checked' if s in chosen else ''}> <code>{s}</code></label></div>"
        for s in OAUTH_SCOPES
    )
    err = f"<p class='err'>{html.escape(error)}</p>" if error else ""
    body = (
        f"<h1>Allow <em>{html.escape(req.client.client_name)}</em> to use your call center?</h1>"
        "<p>Sign in to approve. You will be sent back to the application afterwards.</p>"
        f"{err}<form method='post' action='/oauth/authorize' autocomplete='off'>"
        f"{req.hidden_fields(params)}"
        f"<label for='email'>Email</label><input id='email' type='email' name='email' required value='{html.escape(email, quote=True)}'>"
        "<label for='password'>Password</label><input id='password' type='password' name='password' required>"
        f"<label for='slug'>Workspace (only if you belong to several)</label><input id='slug' type='text' name='tenant_slug' value='{html.escape(slug, quote=True)}'>"
        "<label for='totp'>Authentication code (if 2FA is on)</label><input id='totp' type='text' name='totp_code' inputmode='numeric'>"
        f"<label>Access to grant</label>{boxes}"
        "<button type='submit' name='action' value='approve'>Approve</button>"
        "<button type='submit' name='action' value='deny' formnovalidate>Deny</button></form>"
    )
    return _page(body)


@router.get("/oauth/authorize")
async def authorize_page(request: Request, db: AsyncSession = Depends(get_db)):
    params = dict(request.query_params)
    req, failure = await _validate_authorize(db, params)
    if failure is not None:
        return failure
    return _form(req, params)


@router.post("/oauth/authorize", dependencies=[Depends(ip_rate_limit("oauth-authorize", limit=10))])
async def authorize_submit(request: Request, db: AsyncSession = Depends(get_db)):
    form = await request.form()
    params = {k: v for k, v in form.items() if isinstance(v, str)}
    req, failure = await _validate_authorize(db, params)
    if failure is not None:
        return failure
    state = {"state": req.state} if req.state else {}
    if params.get("action") == "deny":
        return _redirect(req.redirect_uri, {"error": "access_denied", **state})

    email = params.get("email", "").strip().lower()
    slug = params.get("tenant_slug", "").strip()

    def fail(message: str) -> HTMLResponse:
        page = _form(req, params, message, email, slug)
        page.status_code = 401
        return page

    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        burn_password_check(params.get("password", ""))
    if user is None or not user.is_active or not verify_password(params.get("password", ""), user.hashed_password):
        return fail("Invalid email or password.")
    totp = await get_enabled_totp(db, user.id)
    if totp is not None:
        try:
            ok = bool(params.get("totp_code")) and await verify_second_factor(db, totp, params["totp_code"])
        except HTTPException as exc:
            return fail(str(exc.detail))
        if not ok:
            return fail("Invalid authentication code.")

    query = (
        select(TenantUser)
        .join(Tenant, TenantUser.tenant_id == Tenant.id)
        .where(
            TenantUser.user_id == user.id,
            TenantUser.status == MembershipStatus.ACTIVE,
            Tenant.status.in_([TenantStatus.ACTIVE, TenantStatus.TRIAL]),
        )
        .options(joinedload(TenantUser.role).joinedload(Role.permissions))
    )
    if slug:
        query = query.where(Tenant.slug == slug)
    memberships = (await db.execute(query)).unique().scalars().all()
    if not memberships:
        return fail("No active workspace found for this account.")
    if len(memberships) > 1:
        return fail("You belong to several workspaces; enter the workspace name above.")
    membership = memberships[0]

    permissions = {p.code for p in membership.role.permissions}
    if not (user.is_platform_admin or Permissions.MCP_MANAGE in permissions):
        return fail("Your role is not allowed to connect external AI clients (needs mcp.manage).")
    granted = [
        s
        for s in dict.fromkeys(form.getlist("grant"))
        if s in OAUTH_SCOPES
        and (user.is_platform_admin or SCOPE_PERMISSIONS[s] <= permissions)
    ]
    if not granted:
        return fail("Select at least one access scope your role allows.")

    plaintext, hashed = new_secret("")
    db.add(
        McpOAuthCode(
            tenant_id=membership.tenant_id,
            hashed_code=hashed,
            client_id=req.client.id,
            user_id=user.id,
            scopes=granted,
            redirect_uri=req.redirect_uri,
            code_challenge=req.challenge,
            resource=req.resource,
            expires_at=datetime.now(UTC) + timedelta(seconds=CODE_SECONDS),
        )
    )
    await log_audit_event(
        db,
        tenant_id=membership.tenant_id,
        actor_user_id=user.id,
        action="mcp_oauth.authorized",
        resource_type="mcp_oauth_client",
        resource_id=req.client.id,
        extra={"scopes": granted, "client_name": req.client.client_name},
    )
    await db.commit()
    return _redirect(req.redirect_uri, {"code": plaintext, **state})


# ---------------------------------------------------------------- token


async def _issue_tokens(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    user_id: uuid.UUID,
    scopes: list[str],
    resource: str | None,
) -> JSONResponse:
    access, access_hash = new_secret(ACCESS_PREFIX)
    refresh, refresh_hash = new_secret(REFRESH_PREFIX)
    now = datetime.now(UTC)
    db.add(
        McpOAuthToken(
            tenant_id=tenant_id,
            hashed_access_token=access_hash,
            hashed_refresh_token=refresh_hash,
            client_id=client_id,
            user_id=user_id,
            scopes=scopes,
            resource=resource,
            access_expires_at=now + timedelta(seconds=ACCESS_TOKEN_SECONDS),
            refresh_expires_at=now + timedelta(days=REFRESH_TOKEN_DAYS),
        )
    )
    await db.commit()
    return _json(
        {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_SECONDS,
            "refresh_token": refresh,
            "scope": " ".join(scopes),
        }
    )


@router.post("/oauth/token", dependencies=[Depends(ip_rate_limit("oauth-token", limit=30))])
async def token_endpoint(request: Request, db: AsyncSession = Depends(get_db)) -> JSONResponse:
    form = await request.form()
    p = {k: v for k, v in form.items() if isinstance(v, str)}
    try:
        client_id = uuid.UUID(p.get("client_id", ""))
    except ValueError:
        return _oauth_error("invalid_client", "Unknown client_id", 401)
    client = await db.get(McpOAuthClient, client_id)
    if client is None:
        return _oauth_error("invalid_client", "Unknown client_id", 401)
    if not same_resource(p.get("resource")):
        return _oauth_error("invalid_target", "Unknown resource")

    grant = p.get("grant_type")
    now = datetime.now(UTC)
    if grant == "authorization_code":
        code = (
            await db.execute(
                select(McpOAuthCode).where(McpOAuthCode.hashed_code == hash_token(p.get("code", "")))
            )
        ).scalar_one_or_none()
        if (
            code is None
            or code.client_id != client.id
            or code.expires_at <= now
            or code.redirect_uri != p.get("redirect_uri")
            or not pkce_matches(p.get("code_verifier", ""), code.code_challenge)
        ):
            return _oauth_error("invalid_grant", "Invalid, expired or mismatched authorization code")
        # Atomic single use: only the request that flips used_at may proceed.
        claimed = await db.execute(
            update(McpOAuthCode)
            .where(McpOAuthCode.id == code.id, McpOAuthCode.used_at.is_(None))
            .values(used_at=now)
        )
        if claimed.rowcount != 1:
            return _oauth_error("invalid_grant", "Authorization code already used")
        return await _issue_tokens(
            db,
            tenant_id=code.tenant_id,
            client_id=client.id,
            user_id=code.user_id,
            scopes=list(code.scopes),
            resource=code.resource or p.get("resource"),
        )

    if grant == "refresh_token":
        row = (
            await db.execute(
                select(McpOAuthToken).where(
                    McpOAuthToken.hashed_refresh_token == hash_token(p.get("refresh_token", ""))
                )
            )
        ).scalar_one_or_none()
        if row is None or row.client_id != client.id:
            return _oauth_error("invalid_grant", "Invalid refresh token")
        if row.revoked_at is not None:
            # A rotated/revoked refresh token came back: assume it leaked and
            # kill every live token for this client+user.
            await db.execute(
                update(McpOAuthToken)
                .where(
                    McpOAuthToken.client_id == row.client_id,
                    McpOAuthToken.user_id == row.user_id,
                    McpOAuthToken.tenant_id == row.tenant_id,
                    McpOAuthToken.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            await db.commit()
            return _oauth_error("invalid_grant", "Refresh token was already used")
        user = await db.get(User, row.user_id)
        if row.refresh_expires_at <= now or user is None or not user.is_active:
            return _oauth_error("invalid_grant", "Refresh token expired")
        row.revoked_at = now
        return await _issue_tokens(
            db,
            tenant_id=row.tenant_id,
            client_id=client.id,
            user_id=row.user_id,
            scopes=list(row.scopes),
            resource=row.resource,
        )

    return _oauth_error("unsupported_grant_type", "Use authorization_code or refresh_token")


@router.post("/oauth/revoke", dependencies=[Depends(ip_rate_limit("oauth-token", limit=30))])
async def revoke_endpoint(request: Request, db: AsyncSession = Depends(get_db)) -> JSONResponse:
    """RFC 7009: always 200, whether or not the token existed."""
    form = await request.form()
    token = str(form.get("token") or "")
    if token:
        hashed = hash_token(token)
        await db.execute(
            update(McpOAuthToken)
            .where(
                (McpOAuthToken.hashed_access_token == hashed)
                | (McpOAuthToken.hashed_refresh_token == hashed),
                McpOAuthToken.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(UTC))
        )
        await db.commit()
    return _json({})
