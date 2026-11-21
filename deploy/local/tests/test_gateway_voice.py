"""The live-voice door: /voice/ws on the gateway. Real sockets, a fake voice agent behind it."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, WebSocket
from websockets.sync.client import connect
from websockets.exceptions import InvalidStatus, ConnectionClosed

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_gateway import Server, free_port, write_user, TOK  # noqa: E402
import gateway  # noqa: E402


def fake_agent() -> FastAPI:
    app = FastAPI()

    @app.websocket("/ws")
    async def ws(w: WebSocket):
        await w.accept()
        await w.send_text(json.dumps({"t": "hello", "port": w.query_params.get("port"), "token_seen": w.query_params.get("token")}))
        while True:
            m = await w.receive()
            if m["type"] == "websocket.disconnect":
                return
            if m.get("bytes") is not None:
                await w.send_bytes(b"\x01" + m["bytes"])
            elif m.get("text") is not None:
                await w.send_text(m["text"])

    return app


@pytest.fixture()
def door(tmp_path, monkeypatch):
    users = tmp_path / "users"
    ports = {"alice": free_port(), "bob": free_port()}
    for n in ports:
        write_user(users, n, ports[n], TOK[n])
    vp = free_port()
    monkeypatch.setenv("VOICE_PORT", str(vp))
    with Server(fake_agent(), vp):
        with Server(gateway.create_app(users), free_port()) as gw:
            yield {"gw": gw.port, "ports": ports}


def test_each_person_reaches_the_agent_with_their_own_backend_port_and_audio_flows_both_ways(door):
    with connect(f"ws://127.0.0.1:{door['gw']}/voice/ws?token={TOK['bob']}", max_size=None) as c:
        hello = json.loads(c.recv(timeout=5))
        assert hello["port"] == str(door["ports"]["bob"])               # the gateway, not the phone, says which backend
        c.send(b"\x00\x01\x02\x03")
        assert c.recv(timeout=5) == b"\x01\x00\x01\x02\x03"              # binary audio both ways
        c.send(json.dumps({"type": "interrupt"}))
        assert json.loads(c.recv(timeout=5)) == {"type": "interrupt"}    # text control messages too


def test_a_phone_cannot_choose_the_backend(door):
    with connect(f"ws://127.0.0.1:{door['gw']}/voice/ws?token={TOK['alice']}&port={door['ports']['bob']}") as c:
        assert json.loads(c.recv(timeout=5))["port"] == str(door["ports"]["alice"])


def test_a_wrong_or_missing_key_is_refused_before_anything_connects(door):
    for q in ("token=nope", ""):
        with pytest.raises((InvalidStatus, ConnectionClosed)):
            with connect(f"ws://127.0.0.1:{door['gw']}/voice/ws?{q}") as c:
                c.recv(timeout=3)


def test_when_the_voice_agent_is_off_the_connection_fails_fast_so_the_app_can_fall_back(tmp_path, monkeypatch):
    users = tmp_path / "users"
    write_user(users, "alice", free_port(), TOK["alice"])
    monkeypatch.setenv("VOICE_PORT", str(free_port()))                   # nothing listens there
    with Server(gateway.create_app(users), free_port()) as gw:
        with pytest.raises((InvalidStatus, ConnectionClosed)):
            with connect(f"ws://127.0.0.1:{gw.port}/voice/ws?token={TOK['alice']}", open_timeout=8) as c:
                c.recv(timeout=3)
