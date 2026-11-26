"""Streaming model client for the Conversation Core (Gemini through its OpenAI-compatible endpoint, free tier).

* Reads the live model list (`GET /models`) so a model name that no longer exists is never tried; a 404 marks a name dead for six hours.
* One fallback chain: configured model first, then the others. 429 puts a model in the shared cooldown (`backend.agents.base`), 5xx / timeouts / empty replies
  fall through to the next model; a 400/401/403 stops the chain (no point retrying).
* Streams: text arrives as deltas, a tool call arrives when its JSON arguments are complete. Gemini 3 hands back a `thought_signature` that must be passed back with the
  call (`extra_content`), so tool calls keep it.
* Never logs or returns the API key.

Events yielded by `stream()`:
    {"k": "text", "d": str}
    {"k": "tool", "id": str, "name": str, "args": dict, "extra": dict | None}
    {"k": "end", "model": str, "finish": str | None, "usage": dict | None, "ttft": float | None}
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Iterator

import httpx

from backend.agents import base

DEAD_SECS = 6 * 3600.0
LIST_TTL = 3600.0
_lock = threading.Lock()
_models: dict = {"ts": 0.0, "ids": None}
_dead: dict[str, float] = {}

# What the chain tries, best first, when CONVO_MODELS is not set. Anything not in the live list is skipped.
DEFAULT_CHAIN = ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.5-flash", "gemini-flash-lite-latest", "gemini-flash-latest")
_BAD_FOR_CHAT = re.compile(r"tts|image|live|audio|embed|imagen|veo|lyria|omni|robotics|computer|banana|customtools|learnlm|gemma|aqa|vision")


class LLMUnavailable(RuntimeError):
    """Every model in the chain failed (or no key). The caller falls back to the legacy brain."""


def have_key() -> bool:
    return bool(base._cfg("compose")["api_key"])


def _endpoint() -> tuple[str, dict]:
    c = base._cfg("compose")
    return c["base_url"], {"Authorization": f"Bearer {c['api_key']}", "Content-Type": "application/json"}


def available_models(force: bool = False) -> list[str] | None:
    """Ids the key can use (None if the list cannot be read: then nothing is filtered). Cached for an hour."""
    now = time.time()
    with _lock:
        if not force and _models["ids"] is not None and now - _models["ts"] < LIST_TTL:
            return _models["ids"]
    if not have_key():
        return None
    url, headers = _endpoint()
    try:
        r = base._http().get(f"{url}/models", headers=headers, timeout=8.0)
        if r.status_code != 200:
            return None
        ids = sorted(m["id"].replace("models/", "") for m in r.json().get("data", []) if isinstance(m, dict) and m.get("id"))
    except (httpx.HTTPError, ValueError, KeyError):
        return None
    with _lock:
        _models.update(ts=now, ids=ids)
    return ids


def model_chain() -> list[str]:
    """Models to try, in order. CONVO_MODELS (comma list) overrides; otherwise the configured compose model + its fallbacks + DEFAULT_CHAIN."""
    env = [m.strip() for m in (os.environ.get("CONVO_MODELS") or "").split(",") if m.strip()]
    c = base._cfg("compose")
    want = env or [c["model"], *c["fallbacks"], *DEFAULT_CHAIN]
    seen: list[str] = []
    for m in want:
        if m and m not in seen and not _BAD_FOR_CHAT.search(m):
            seen.append(m)
    live = available_models()
    if live:
        seen = [m for m in seen if m in live] or seen
    now = time.time()
    ok = [m for m in seen if _dead.get(m, 0.0) <= now and not base._cooling(m)]
    return ok or [m for m in seen if _dead.get(m, 0.0) <= now] or seen


def mark_dead(model: str) -> None:
    _dead[model] = time.time() + DEAD_SECS


def dead_models() -> dict[str, int]:
    now = time.time()
    return {m: int(t - now) for m, t in _dead.items() if t > now}


def reasoning_effort() -> str:
    return (os.environ.get("CONVO_REASONING") or "minimal").strip()


def openai_tools(specs: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("parameters") or {"type": "object", "properties": {}}}}
            for t in specs]


def _open(model: str, body: dict, timeout: float):
    """Open the streaming HTTP response (a test replaces this). Returns a context manager yielding the response."""
    url, headers = _endpoint()
    return base._http().stream("POST", f"{url}/chat/completions", json={**body, "model": model}, headers=headers,
                               timeout=httpx.Timeout(timeout, connect=min(timeout, 3.0)))


def _stream_one(model: str, body: dict, timeout: float, cancel: threading.Event | None) -> Iterator[dict]:
    t0 = time.time()
    calls: dict[int, dict] = {}
    done_calls: set[int] = set()
    ttft = None
    finish = None
    usage = None
    got_any = False
    with _open(model, body, timeout) as resp:
        if resp.status_code != 200:
            resp.read()
            raise _HTTPFail(model, resp.status_code, resp.text[:300])
        for line in resp.iter_lines():
            if cancel is not None and cancel.is_set():
                return
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            if chunk.get("usage"):
                usage = chunk["usage"]
            for ch in chunk.get("choices") or []:
                d = ch.get("delta") or {}
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
                txt = d.get("content")
                if txt:
                    if ttft is None:
                        ttft = time.time() - t0
                    got_any = True
                    yield {"k": "text", "d": txt}
                for tc in d.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    cur = calls.setdefault(idx, {"id": "", "name": "", "args": "", "extra": None})
                    if tc.get("id"):
                        cur["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        cur["name"] = fn["name"]
                    if fn.get("arguments"):
                        cur["args"] += fn["arguments"]
                    if tc.get("extra_content"):
                        cur["extra"] = tc["extra_content"]
                    if idx not in done_calls and cur["name"]:
                        try:
                            args = json.loads(cur["args"] or "{}")
                        except ValueError:
                            continue
                        done_calls.add(idx)
                        if ttft is None:
                            ttft = time.time() - t0
                        got_any = True
                        yield {"k": "tool", "id": cur["id"] or f"call_{idx}", "name": cur["name"], "args": args if isinstance(args, dict) else {}, "extra": cur["extra"]}
    for idx, cur in calls.items():              # arguments that only became valid JSON at the very end
        if idx not in done_calls and cur["name"]:
            try:
                args = json.loads(cur["args"] or "{}")
            except ValueError:
                args = {}
            got_any = True
            yield {"k": "tool", "id": cur["id"] or f"call_{idx}", "name": cur["name"], "args": args if isinstance(args, dict) else {}, "extra": cur["extra"]}
    if not got_any:
        raise _HTTPFail(model, 200, "empty reply (no text and no tool call)")
    yield {"k": "end", "model": model, "finish": finish, "usage": usage, "ttft": ttft}


class _HTTPFail(Exception):
    def __init__(self, model: str, status: int, text: str):
        super().__init__(f"{model}: HTTP {status}: {text[:160]}")
        self.model, self.status, self.text = model, status, text


def stream(messages: list[dict], tools: list[dict] | None = None, *, max_tokens: int = 320, temperature: float = 0.7, timeout: float = 8.0,
           models: list[str] | None = None, cancel: threading.Event | None = None, reasoning: str | None = None) -> Iterator[dict]:
    """Stream one model turn through the fallback chain. Raises LLMUnavailable if no model produced anything usable.
    A failure AFTER the first event of a model is not retried (the caller has already used the earlier output)."""
    if not have_key():
        raise LLMUnavailable("no API key configured")
    last = ""
    chain = models or model_chain()
    effort = reasoning if reasoning is not None else reasoning_effort()
    for model in chain:
        body: dict = {"messages": messages, "stream": True, "max_tokens": max_tokens, "temperature": temperature}
        if tools:
            body["tools"] = tools
        if effort:
            body["reasoning_effort"] = effort
        started = False
        for attempt in range(2):
            try:
                for ev in _stream_one(model, body, timeout, cancel):
                    started = True
                    yield ev
                return
            except _HTTPFail as e:
                last = str(e)
                if started:
                    raise LLMUnavailable(last)
                if e.status == 404:
                    mark_dead(model)
                    break
                if e.status == 429:
                    base._cool_down(model, e.text)
                    break
                if e.status == 400 and "reasoning" in e.text.lower() and "reasoning_effort" in body:
                    body.pop("reasoning_effort")           # this model does not take the knob: once more without it
                    continue
                if e.status in (400, 401, 403):
                    raise LLMUnavailable(f"{model} rejected the request: HTTP {e.status}")
                break                                       # 5xx / empty: next model
            except (httpx.HTTPError, OSError) as e:
                last = f"{model}: {type(e).__name__}"
                if started:
                    raise LLMUnavailable(last)
                break
    raise LLMUnavailable(f"all models failed; last error: {last}")
