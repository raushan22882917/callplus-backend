"""ASGI gate in front of the MCP app: unauthenticated or invalid-token
requests get an HTTP 401 with the RFC 9728 `WWW-Authenticate` challenge, which
is how an OAuth-capable MCP host discovers where to sign in. Static API keys
keep working unchanged (they just need to be present)."""

import json

from app.core.mcp_oauth import ACCESS_PREFIX, issuer
from app.mcp.auth import _bearer_token, valid_oauth_token
from starlette.types import ASGIApp, Receive, Scope, Send

from packages.database.session import session_scope


class McpAuthGate:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        token = _bearer_token(headers)
        problem = None if token else "missing"
        if token and token.startswith(ACCESS_PREFIX):
            async with session_scope() as db:
                if await valid_oauth_token(db, token) is None:
                    problem = "invalid_token"
        if problem is None:
            await self.app(scope, receive, send)
            return

        challenge = f'Bearer resource_metadata="{issuer()}/.well-known/oauth-protected-resource"'
        if problem == "invalid_token":
            challenge += ', error="invalid_token"'
        body = json.dumps({"error": "unauthorized", "error_description": "Bearer token required"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", challenge.encode("latin-1")),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
