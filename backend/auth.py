"""Optional bearer-token auth. API_TOKEN unset/blank -> no-op (behaviour identical to before). Set -> every route except
/health (and CORS preflight OPTIONS) needs `Authorization: Bearer <token>`. Read per request so it can be toggled without code."""
from __future__ import annotations

import hmac
import os

from starlette.responses import JSONResponse

OPEN_PATHS = {"/health"}


def api_token() -> str:
    return (os.environ.get("API_TOKEN") or "").strip()


class TokenAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        want = api_token() if scope["type"] in ("http", "websocket") else ""
        if not want or scope["type"] != "http" or scope["method"] == "OPTIONS" or scope["path"] in OPEN_PATHS:
            return await self.app(scope, receive, send)
        got = ""
        for k, v in scope.get("headers", []):
            if k == b"authorization":
                got = v.decode("latin-1")
                break
        scheme, _, tok = got.partition(" ")
        if scheme.lower() == "bearer" and hmac.compare_digest(tok.strip().encode(), want.encode()):
            return await self.app(scope, receive, send)
        await JSONResponse({"detail": "Unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
