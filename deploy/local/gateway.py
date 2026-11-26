"""Front door: one public port, the Bearer token picks which person's backend gets the request.
Run:  python deploy/local/gateway.py [--host 127.0.0.1] [--port 8000]   (run.py does this for you)
Never logs tokens or Authorization headers; the backends additionally enforce their own API_TOKEN."""
from __future__ import annotations

import argparse
import asyncio
import hmac
import os
import sys
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, StreamingResponse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "trailers",
       "transfer-encoding", "upgrade", "proxy-connection"}
METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]
TIMEOUT = httpx.Timeout(120.0, connect=5.0)


class Users:
    """token -> port map over one or more users dirs (deploy/local/users and deploy/users from new_user.py: both are
    <name>.json with port + token). Re-read whenever any file's (name, mtime, size) changes -> no restart for new people."""

    def __init__(self, directories=None):
        if directories is None:
            self.dirs = None
        elif isinstance(directories, (str, Path)):
            self.dirs = [Path(directories)]
        else:
            self.dirs = [Path(d) for d in directories]
        self._sig: tuple = ()
        self._rows: list[tuple[bytes, int]] = []

    def _dirs(self) -> list[Path]:
        return self.dirs if self.dirs is not None else default_user_dirs()

    def _signature(self) -> tuple:
        sig = []
        for d in self._dirs():
            if not d.is_dir():
                continue
            for p in d.glob("*.json"):
                try:
                    st = p.stat()
                    sig.append((str(p), st.st_mtime_ns, st.st_size))
                except OSError:
                    pass
        return tuple(sorted(sig))

    def port_for(self, token: str) -> int | None:
        sig = self._signature()
        if sig != self._sig:
            rows = []
            for d in self._dirs():
                rows += [(u["token"].encode(), u["port"]) for u in common.load_users(d).values()]
            self._rows, self._sig = rows, sig
        found = None
        tok = token.encode()
        for t, port in self._rows:  # no early exit: every candidate compared in constant time
            if hmac.compare_digest(tok, t):
                found = port
        return found


def default_user_dirs() -> list[Path]:
    """GATEWAY_USERS_DIRS (os.pathsep-separated) or: deploy/users (Docker, new_user.py) + deploy/local/users."""
    env = os.environ.get("GATEWAY_USERS_DIRS")
    if env:
        return [Path(p) for p in env.split(os.pathsep) if p]
    return [common.ROOT / "deploy" / "users", common.users_dir()]


class _Close:
    def __init__(self, up):
        self.up = up

    async def __call__(self):
        await self.up.aclose()


def create_app(users_dir=None, upstream_host: str = "127.0.0.1") -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    users = Users(users_dir)
    client = httpx.AsyncClient(timeout=TIMEOUT, limits=httpx.Limits(max_connections=100))

    @app.on_event("shutdown")
    async def _close():
        await client.aclose()

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/voice/web")
    async def voice_web():
        """A voice page for the phone's browser: audio goes through the normal media route, so earphones work (the app's own audio module forces the loudspeaker).
        The page itself holds no secret; it asks for the access key once and remembers it on that phone."""
        from fastapi.responses import FileResponse
        return FileResponse(Path(__file__).with_name("voice_web.html"), media_type="text/html", headers={"Cache-Control": "no-store"})

    @app.websocket("/voice/ws")
    async def voice_ws(ws: WebSocket):
        """Live voice: the phone streams microphone audio, the PC's voice agent (GPU) answers with audio. The access key picks the person's
        backend exactly as for the HTTP routes; the voice agent itself only listens on 127.0.0.1."""
        import websockets
        tok = ws.query_params.get("token", "").strip()
        port = users.port_for(tok) if tok else None
        if port is None:
            await ws.close(code=4401)
            return
        try:
            up = await websockets.connect(f"ws://{upstream_host}:{common.voice_port()}/ws?token={tok}&port={port}", max_size=None, open_timeout=5)
        except Exception:
            await ws.close(code=4503)            # the voice agent is not running: the app falls back to its built-in voice
            return
        await ws.accept()

        async def down():
            async for m in up:
                if isinstance(m, bytes):
                    await ws.send_bytes(m)
                else:
                    await ws.send_text(m)

        async def upl():
            try:
                while True:
                    m = await ws.receive()
                    if m["type"] == "websocket.disconnect":
                        break
                    if m.get("bytes") is not None:
                        await up.send(m["bytes"])
                    elif m.get("text") is not None:
                        await up.send(m["text"])
            except WebSocketDisconnect:
                pass

        tasks = [asyncio.create_task(down()), asyncio.create_task(upl())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await up.close()
            try:
                await ws.close()
            except Exception:
                pass

    @app.options("/{path:path}")
    async def preflight(request: Request):
        return Response(status_code=204, headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": request.headers.get("access-control-request-headers", "authorization, content-type"),
            "Access-Control-Max-Age": "600"})

    @app.api_route("/{path:path}", methods=METHODS)
    async def proxy(path: str, request: Request):
        scheme, _, tok = request.headers.get("authorization", "").partition(" ")
        port = users.port_for(tok.strip()) if scheme.lower() == "bearer" and tok.strip() else None
        if port is None:
            return JSONResponse({"detail": "Unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
        hdrs = [(k, v) for k, v in request.headers.items() if k.lower() not in HOP and k.lower() != "host"]
        url = httpx.URL(f"http://{upstream_host}:{port}/{path}", query=request.url.query.encode())
        has_body = request.method in ("POST", "PUT", "PATCH", "DELETE") and (
            "content-length" in request.headers or "transfer-encoding" in request.headers)
        try:
            up = await client.send(client.build_request(
                request.method, url, headers=hdrs, content=request.stream() if has_body else None), stream=True)
        except httpx.TimeoutException:
            return JSONResponse({"detail": "Backend timed out"}, status_code=504)
        except httpx.HTTPError:
            return JSONResponse({"detail": "Your coach backend is not reachable (starting up or down)"}, status_code=502)
        out = [(k, v) for k, v in up.headers.multi_items() if k.lower() not in HOP]
        if request.method == "HEAD":
            await up.aclose()
            return Response(status_code=up.status_code, headers=dict(out))
        resp = StreamingResponse(up.aiter_raw(), status_code=up.status_code, background=_Close(up))
        resp.raw_headers = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in out]
        return resp

    return app


def main() -> int:
    import uvicorn
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=common.gateway_port())
    ap.add_argument("--users-dir", action="append", help="repeatable; default deploy/users + deploy/local/users")
    a = ap.parse_args()
    uvicorn.run(create_app(a.users_dir), host=a.host, port=a.port, log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
