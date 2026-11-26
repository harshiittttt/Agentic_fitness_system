"""A 'brain' takes what the athlete said and returns what the coach answers. Today: the legacy brain (POST /voice/command).
Later: the converse brain (POST /convo/turn, NDJSON events ack|say|action|state|done; see docs/CONVERSATIONAL_AGENT_PLAN.md phase 1).
Both return the same dict so the scorers do not care which one produced it:
    {reply, intent, actions, expects, ms (request -> full reply), first_word_ms (request -> first words; = ms for non-streaming), raw}"""
from __future__ import annotations

import json
import os
import time

import httpx

TOKEN = os.environ.get("EVAL_TOKEN", "testtoken")


class BrainUnavailable(Exception):
    pass


class LegacyBrain:
    kind = "legacy"

    def __init__(self, name: str, url: str):
        self.name, self.url = name, url.rstrip("/")
        self.h = httpx.Client(base_url=self.url, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=40.0)

    def turn(self, text: str, lang: str | None = None, screen: str = "live", ambient: bool = False) -> dict:
        t0 = time.perf_counter()
        r = self.h.post("/voice/command", json={"transcript": text, "screen": screen, "ambient": ambient, "lang": lang})
        ms = (time.perf_counter() - t0) * 1000
        r.raise_for_status()
        d = r.json()
        return {"reply": d.get("reply") or "", "intent": d.get("intent"), "actions": d.get("actions") or [], "expects": d.get("expects"),
                "ms": ms, "first_word_ms": ms, "raw": d}


class ConverseBrain:
    """Client for POST /convo/turn (docs/CONVO_CONTRACTS.md): text, turn_id, conn_id, lang -> NDJSON ack|tool|action|state|say|done.
    One conn_id per scenario (new_conversation) so the dialogue history never leaks between scenarios."""
    kind = "converse"

    def __init__(self, name: str, url: str):
        self.name, self.url = name, url.rstrip("/")
        self.h = httpx.Client(base_url=self.url, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=40.0)
        st = self.h.get("/convo/status")
        if st.status_code in (404, 405):
            raise BrainUnavailable(f"{self.url}/convo/status does not exist (the Conversation Core is not in this backend)")
        self.conn = "eval-0"
        self.n = 0
        self.seq = 0

    def new_conversation(self) -> None:
        self.seq += 1
        self.n = 0
        self.conn = f"eval-{os.getpid()}-{self.seq}"
        try:
            self.h.post("/convo/reset", json={"conn_id": self.conn})
        except Exception:                                                        # noqa: BLE001
            pass

    def turn(self, text: str, lang: str | None = None, screen: str = "live", ambient: bool = False) -> dict:
        self.n += 1
        t0 = time.perf_counter()
        said: list[str] = []
        actions: list = []
        first = None
        intent = None
        done: dict = {}
        body = {"text": text, "turn_id": f"{self.conn}-t{self.n}", "conn_id": self.conn, "lang": lang, "screen": screen, "ambient": ambient}
        with self.h.stream("POST", "/convo/turn", params={"brain": "converse"}, json=body) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line.strip():
                    continue
                ev = json.loads(line)
                k = ev.get("t")
                if k in ("say", "ack"):
                    if first is None:
                        first = (time.perf_counter() - t0) * 1000
                    if k == "say":
                        said.append(ev.get("text", ""))
                elif k == "action":
                    actions.append(ev.get("action") or ev)
                elif k == "done":
                    done = ev
                    intent = ev.get("intent", intent)
        ms = (time.perf_counter() - t0) * 1000
        reply = (done.get("said") or " ".join(said)).strip()
        return {"reply": reply, "intent": intent, "actions": actions, "expects": done.get("expects"), "ms": ms, "first_word_ms": first or ms, "raw": done}


def make(spec: str):
    """'legacy=http://127.0.0.1:8011' | 'converse=http://127.0.0.1:8011' | 'http://127.0.0.1:8011' (legacy) -> a brain."""
    name, _, url = spec.partition("=")
    if not url:
        name, url = "legacy", spec
    cls = ConverseBrain if name.startswith("conv") else LegacyBrain
    return cls(name, url)
