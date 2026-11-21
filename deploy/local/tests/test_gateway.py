"""Gateway tests: two fake upstream backends + the real gateway, all on real local sockets (uvicorn in threads)."""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

LOCAL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LOCAL))
import common  # noqa: E402
import gateway  # noqa: E402

TOK = {"alice": "tok-alice-" + "a" * 30, "bob": "tok-bob-" + "b" * 30}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fake_backend(name: str, token: str) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def auth(request: Request, call_next):  # like backend.auth: own token enforced too
        if request.url.path != "/health" and request.headers.get("authorization") != f"Bearer {token}":
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        return await call_next(request)

    @app.api_route("/echo", methods=["GET", "POST", "PUT", "DELETE"])
    async def echo(request: Request):
        body = (await request.body()).decode()
        return {"who": name, "method": request.method, "query": dict(request.query_params), "body": body,
                "headers": {k.lower(): v for k, v in request.headers.items()}}

    @app.get("/stream")
    async def stream():
        async def gen():
            for i in range(3):
                yield f"chunk{i};".encode()
                await _sleep(0.6)
        return StreamingResponse(gen(), media_type="audio/mpeg", headers={"X-Who": name})

    @app.get("/teapot")
    async def teapot():
        return JSONResponse({"who": name}, status_code=418, headers={"Connection": "close", "X-Custom": "yes"})

    return app


async def _sleep(t):
    import asyncio
    await asyncio.sleep(t)


class Server:
    def __init__(self, app, port):
        self.port = port
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("server did not start")

    def __exit__(self, *a):
        self.server.should_exit = True
        self.thread.join(5)


def write_user(d: Path, name: str, port: int, token: str):
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps({"name": name, "port": port, "token": token, "data_dir": "x"}), encoding="utf-8")


@pytest.fixture()
def stack(tmp_path):
    ports = {"alice": free_port(), "bob": free_port()}
    users = tmp_path / "users"
    for n in ports:
        write_user(users, n, ports[n], TOK[n])
    with Server(fake_backend("alice", TOK["alice"]), ports["alice"]), Server(fake_backend("bob", TOK["bob"]), ports["bob"]):
        gp = free_port()
        with Server(gateway.create_app(users), gp):
            yield {"url": f"http://127.0.0.1:{gp}", "users": users, "ports": ports}


def H(name):
    return {"Authorization": f"Bearer {TOK[name]}"}


def test_each_token_reaches_only_its_backend(stack):
    with httpx.Client(base_url=stack["url"]) as c:
        assert c.get("/echo", headers=H("alice")).json()["who"] == "alice"
        assert c.get("/echo", headers=H("bob")).json()["who"] == "bob"


def test_bad_or_missing_token_is_401(stack):
    with httpx.Client(base_url=stack["url"]) as c:
        for h in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic abc"}, {"Authorization": TOK["alice"]}):
            r = c.get("/echo", headers=h)
            assert r.status_code == 401, h
            assert r.headers["www-authenticate"] == "Bearer"


def test_health_is_open_and_does_not_touch_backends(stack):
    # stop nothing: just prove no token is needed and the body is the gateway's own
    r = httpx.get(stack["url"] + "/health")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_health_works_with_all_backends_down(tmp_path):
    users = tmp_path / "users"
    write_user(users, "ghost", free_port(), "tok-ghost-" + "g" * 30)  # nothing listens there
    with Server(gateway.create_app(users), free_port()) as gw:
        assert httpx.get(f"http://127.0.0.1:{gw.port}/health").json() == {"ok": True}
        r = httpx.get(f"http://127.0.0.1:{gw.port}/echo", headers={"Authorization": "Bearer tok-ghost-" + "g" * 30})
        assert r.status_code == 502


def test_preflight_answered_by_gateway(stack):
    r = httpx.options(stack["url"] + "/meal", headers={"Origin": "http://x", "Access-Control-Request-Method": "POST",
                                                        "Access-Control-Request-Headers": "authorization,content-type"})
    assert r.status_code == 204
    assert r.headers["access-control-allow-origin"] == "*"
    assert "authorization" in r.headers["access-control-allow-headers"]


def test_methods_query_body_status_and_headers(stack):
    with httpx.Client(base_url=stack["url"]) as c:
        r = c.post("/echo?a=1&b=two", headers={**H("bob"), "X-Thing": "hello", "Connection": "keep-alive, X-Drop",
                                               "Content-Type": "application/json"}, content=json.dumps({"transcript": "4 eggs"}))
        j = r.json()
        assert j["method"] == "POST" and j["query"] == {"a": "1", "b": "two"} and json.loads(j["body"]) == {"transcript": "4 eggs"}
        assert j["headers"]["x-thing"] == "hello"
        assert j["headers"]["authorization"] == f"Bearer {TOK['bob']}"
        assert j["headers"]["host"].startswith("127.0.0.1:" + str(stack["ports"]["bob"]))  # host rewritten for upstream
        assert c.put("/echo", headers=H("alice"), content="x").json()["method"] == "PUT"
        assert c.delete("/echo", headers=H("alice")).json()["method"] == "DELETE"
        t = c.get("/teapot", headers=H("alice"))
        assert t.status_code == 418 and t.headers["x-custom"] == "yes"
        assert "close" not in t.headers.get("connection", "")  # hop-by-hop response header not forwarded
        assert c.get("/nope", headers=H("alice")).status_code == 404


def test_streaming_arrives_incrementally(stack):
    t0 = time.time()
    with httpx.Client(base_url=stack["url"], timeout=10) as c:
        with c.stream("GET", "/stream", headers=H("alice")) as r:
            assert r.headers["content-type"] == "audio/mpeg" and r.headers["x-who"] == "alice"
            it = r.iter_raw()
            first = next(it)
            t_first = time.time() - t0
            rest = b"".join(it)
    assert first.startswith(b"chunk0;")
    assert t_first < 1.0, f"first chunk should arrive before the stream ends (got it after {t_first:.2f}s)"
    assert (first + rest) == b"chunk0;chunk1;chunk2;"


def test_users_file_hot_reload(stack):
    new_port = free_port()
    with httpx.Client(base_url=stack["url"]) as c:
        carol = "tok-carol-" + "c" * 30
        assert c.get("/echo", headers={"Authorization": f"Bearer {carol}"}).status_code == 401
        with Server(fake_backend("carol", carol), new_port):
            write_user(stack["users"], "carol", new_port, carol)
            assert c.get("/echo", headers={"Authorization": f"Bearer {carol}"}).json()["who"] == "carol"
            # rotate alice's key: old one dies immediately, new one works at the gateway (her fake backend still has the old
            # key, so the backend itself refuses: proves the backend enforces its own token as a second lock)
            new_alice = "tok-alice2-" + "z" * 30
            write_user(stack["users"], "alice", stack["ports"]["alice"], new_alice)
            assert c.get("/echo", headers=H("alice")).status_code == 401
            assert c.get("/echo", headers={"Authorization": f"Bearer {new_alice}"}).status_code == 401  # backend 401
            (stack["users"] / "carol.json").unlink()
            assert c.get("/echo", headers={"Authorization": f"Bearer {carol}"}).status_code == 401


def test_gateway_does_not_log_tokens(stack, capfd):
    with httpx.Client(base_url=stack["url"]) as c:
        c.get("/echo", headers=H("alice"))
        c.get("/echo", headers={"Authorization": "Bearer wrongwrongwrong"})
    out = capfd.readouterr()
    assert TOK["alice"] not in out.out + out.err and "wrongwrongwrong" not in out.out + out.err


def test_users_cli_roundtrip(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DEPLOY_LOCAL_HOME", str(tmp_path))
    monkeypatch.setenv("DEPLOY_LOCAL_FIRST_PORT", "18101")
    import users as cli
    assert cli.cmd_add("anna") == 0 and cli.cmd_add("ben") == 0 and cli.cmd_add("anna") == 1
    u = common.load_users()
    assert u["anna"]["port"] == 18101 and u["ben"]["port"] == 18102 and len(u["anna"]["token"]) >= 43
    assert Path(u["anna"]["data_dir"]) == tmp_path / "data" / "anna" and (tmp_path / "data" / "anna").is_dir()
    old = u["anna"]["token"]
    cli.cmd_rotate("anna")
    assert common.load_users()["anna"]["token"] != old
    assert old not in (tmp_path / "users" / "anna.txt").read_text()
    cli.cmd_remove("ben")
    assert list(common.load_users()) == ["anna"] and (tmp_path / "data" / "ben").is_dir()
    assert cli.cmd_add("Bad Name!") == 2


def test_reads_docker_new_user_format_from_two_dirs(tmp_path):
    """deploy/users/<name>.json from new_user.py has only {port, token} (no name/data_dir)."""
    a, b = tmp_path / "docker", tmp_path / "local"
    pa, pb = free_port(), free_port()
    a.mkdir()
    (a / "dina.json").write_text(json.dumps({"port": pa, "token": "tok-dina-" + "d" * 30}))
    write_user(b, "eli", pb, "tok-eli-" + "e" * 30)
    with Server(fake_backend("dina", "tok-dina-" + "d" * 30), pa), Server(fake_backend("eli", "tok-eli-" + "e" * 30), pb),             Server(gateway.create_app([a, b]), free_port()) as gw:
        c = httpx.Client(base_url=f"http://127.0.0.1:{gw.port}")
        assert c.get("/echo", headers={"Authorization": "Bearer tok-dina-" + "d" * 30}).json()["who"] == "dina"
        assert c.get("/echo", headers={"Authorization": "Bearer tok-eli-" + "e" * 30}).json()["who"] == "eli"
