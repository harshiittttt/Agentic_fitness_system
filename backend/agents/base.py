"""Shared LLM layer used by every agent. Provider-agnostic: anything with an OpenAI-compatible
/chat/completions endpoint works (Gemini, DeepSeek, Groq, OpenRouter, Ollama...).

Roles map to (base_url, api_key, model), each overridable from .env:
    main    agents, tool loops, structured extraction
    fast    short live-coaching lines (low latency)
    critic  safety review; point it at a DIFFERENT provider than `main` for independence

Resolution order for role R and setting S (BASE_URL / API_KEY / MODEL):
    LLM_<R>_<S>  ->  LLM_<S>  ->  built-in Gemini default
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Callable

import httpx
from dotenv import load_dotenv

load_dotenv(override=True)  # project .env wins over stale keys already set in the OS environment

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
# Full-size Flash models have tiny / overloaded free quotas, so lite is the reliable default.
_DEFAULT_MODEL = {"main": "gemini-3.5-flash-lite", "fast": "gemini-3.5-flash-lite", "critic": "gemini-3.5-flash-lite",
                  "compose": "gemini-3.1-flash-lite"}   # the live coach's spoken replies: measured ~1.4 s vs 3-8 s for 3.5-flash-lite
# Per-role defaults: the live coach asks for a SMALL reasoning budget (fast, and no truncated replies from thinking models).
_DEFAULT_REASONING = {"compose": "low"}
_ROLE_FALLBACKS = {"compose": "gemini-3.5-flash-lite,gemini-2.5-flash"}
_DEFAULT_FALLBACKS = "gemini-3.1-flash-lite,gemini-2.5-flash"  # tried in order on 429/5xx; LLM_FALLBACK_MODELS overrides


def _cfg(role: str) -> dict:
    R = role.upper()

    def get(s: str, default: str = "") -> str:
        return (os.environ.get(f"LLM_{R}_{s}") or os.environ.get(f"LLM_{s}") or default).strip()

    return {
        "base_url": get("BASE_URL", GEMINI_URL).rstrip("/"),
        # LLM_API_KEY wins; GEMINI_API_KEY is the zero-config fallback for the default Gemini endpoint
        "api_key": get("API_KEY") or os.environ.get("GEMINI_API_KEY", "").strip(),
        "model": get("MODEL", _DEFAULT_MODEL.get(role, _DEFAULT_MODEL["main"])),
        "reasoning": get("REASONING_EFFORT", _DEFAULT_REASONING.get(role, "")),
        # fallbacks only make sense on the default Gemini endpoint unless the user sets them explicitly
        "fallbacks": [m.strip() for m in get("FALLBACK_MODELS", _ROLE_FALLBACKS.get(role, _DEFAULT_FALLBACKS) if get("BASE_URL", GEMINI_URL).rstrip("/") == GEMINI_URL else "").split(",") if m.strip()],
    }


MODEL = _cfg("main")["model"]
FAST_MODEL = _cfg("fast")["model"]


def have_key(role: str = "main") -> bool:
    return bool(_cfg(role)["api_key"])


class LLMError(RuntimeError):
    pass


USAGE: dict = {"calls": 0, "by_role": {}, "since": None}


def _count(role: str) -> None:
    from datetime import datetime, timezone
    USAGE["since"] = USAGE["since"] or datetime.now(timezone.utc).isoformat(timespec="seconds")
    USAGE["calls"] += 1
    USAGE["by_role"][role] = USAGE["by_role"].get(role, 0) + 1


def _post(role: str, body: dict, timeout: float = 60.0, retries: int = 1) -> dict:
    """Traced wrapper (fitness_os.trace: duration, token usage, outcome; never raises on its own, never records prompt text)."""
    try:
        from fitness_os import trace
        span = trace.span("llm", kind="llm", role=role)
    except Exception:
        return _post_raw(role, body, timeout, retries)
    with span as sp:
        r = _post_raw(role, body, timeout, retries)
        try:
            u = r.get("usage") or {}
            ti, to = u.get("prompt_tokens"), u.get("completion_tokens")
            pin, pout = float(os.environ.get("LLM_PRICE_IN_PER_M", 0) or 0), float(os.environ.get("LLM_PRICE_OUT_PER_M", 0) or 0)
            sp.set(tokens_in=ti, tokens_out=to, model=str(r.get("model") or "")[:48] or None,
                   cost=((ti or 0) * pin + (to or 0) * pout) / 1e6 if (pin or pout) else None)
        except Exception:
            pass
        return r


def _is_empty_reply(data: dict) -> bool:
    """True when a 200 response carries no text and no tool call (so there is nothing for the caller to use)."""
    try:
        msg = (data.get("choices") or [{}])[0].get("message") or {}
    except (AttributeError, IndexError, TypeError):
        return True    # not even a message object: nothing usable
    return not (msg.get("content") or "").strip() and not msg.get("tool_calls")


def _post_raw(role: str, body: dict, timeout: float = 60.0, retries: int = 1) -> dict:
    c = _cfg(role)
    if not c["api_key"]:
        raise LLMError(f"no API key configured for role '{role}' (set GEMINI_API_KEY or LLM_API_KEY in .env)")
    last = ""
    for model in [c["model"], *[m for m in c["fallbacks"] if m != c["model"]]]:
        req = {"model": model, **body}
        if c["reasoning"]:
            req["reasoning_effort"] = c["reasoning"]
        for attempt in range(retries + 1):
            try:
                r = httpx.post(f"{c['base_url']}/chat/completions", json=req, timeout=timeout,
                               headers={"Authorization": f"Bearer {c['api_key']}"})
            except httpx.HTTPError as e:
                last = f"{model}: {type(e).__name__}: {e}"
            else:
                if r.status_code == 200:
                    data = r.json()
                    if _is_empty_reply(data):
                        # HTTP 200 but nothing usable (e.g. a thinking model spent its whole budget on hidden reasoning): not a success.
                        # Retrying the same model would repeat it, so fall through to the next candidate.
                        last = f"{model}: empty reply (no text and no tool call)"
                        break
                    _count(role)
                    return data
                last = f"{model}: HTTP {r.status_code}: {r.text[:200]}"
                if r.status_code not in (429, 500, 502, 503, 504):
                    raise LLMError(f"{c['base_url']} rejected the request: {last}")  # auth / bad request: no point retrying or falling back
            if attempt < retries:
                time.sleep(1.0)
        # this model is exhausted or overloaded: fall through to the next candidate
    raise LLMError(f"all models failed @ {c['base_url']}; last error: {last}")


def _openai_tools(tools: list[dict] | None) -> list[dict]:
    """Tool defs are written in the Anthropic shape {name, description, input_schema}; convert."""
    return [{"type": "function", "function": {
        "name": t["name"], "description": t.get("description", ""),
        "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}} for t in tools or []]


def run_agent(
    system: str,
    user: str,
    tools: list[dict] | None = None,
    handlers: dict[str, Callable[..., dict]] | None = None,
    max_turns: int = 6,
    max_tokens: int = 1024,
    history: list[dict] | None = None,
    trace: list[dict] | None = None,
) -> str:
    """Run a tool-use loop until the model stops calling tools. Returns final text.

    Additive options: `history` = earlier {role: user|assistant, content} turns replayed as real messages before `user`;
    `trace`, if given, is filled with one {tool, args, result} entry per tool call that ran (in order)."""
    handlers = handlers or {}
    oa_tools = _openai_tools(tools)
    messages: list[dict] = [{"role": "system", "content": system}]
    for h in history or []:
        if h.get("role") in ("user", "assistant") and (h.get("content") or "").strip():
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user})
    for _ in range(max_turns):
        body: dict = {"messages": messages, "max_tokens": max_tokens}
        if oa_tools:
            body["tools"] = oa_tools
        msg = _post("main", body)["choices"][0]["message"]
        messages.append(msg)  # keep the raw message: some providers need extra fields echoed back
        calls = msg.get("tool_calls") or []
        if not calls:
            return (msg.get("content") or "").strip()
        for tc in calls:
            fn = handlers.get(tc["function"]["name"])
            args: dict = {}
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
                try:
                    from fitness_os import trace as _tr
                    _span = _tr.span(tc["function"]["name"], kind="tool", tool=tc["function"]["name"])
                except Exception:
                    _span = None
                if _span is None:
                    out = fn(**args) if fn else {"error": f"unknown tool {tc['function']['name']}"}
                else:
                    with _span as _sp:
                        out = fn(**args) if fn else {"error": f"unknown tool {tc['function']['name']}"}
                        if isinstance(out, dict) and out.get("error"):
                            _sp.set(outcome="error", error=out["error"])
            except Exception as e:  # surface tool errors to the model
                out = {"error": str(e)}
            if trace is not None:
                trace.append({"tool": tc["function"]["name"], "args": args, "result": out})
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": json.dumps(out, default=str, ensure_ascii=False)})
    return "(agent stopped: too many tool turns)"


def _json_from_text(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except ValueError:
        return {}


def extract_structured(system: str, user: str, tool: dict, max_tokens: int = 1024) -> dict:
    """Force the model to answer via a single tool call; return its arguments dict.
    Falls back to parsing JSON from plain text if the provider ignores forced tool choice."""
    body = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens, "tools": _openai_tools([tool]),
            "tool_choice": {"type": "function", "function": {"name": tool["name"]}}}
    msg = _post("main", body)["choices"][0]["message"]
    for tc in msg.get("tool_calls") or []:
        try:
            return json.loads(tc["function"].get("arguments") or "{}")
        except ValueError:
            continue
    return _json_from_text(msg.get("content") or "")


def quick_text(system: str, user: str, max_tokens: int = 160, timeout: float = 6.0, role: str = "fast") -> str:
    """One fast, short completion for live coaching. Raises on failure; callers fall back to templates."""
    msg = _post(role, {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                         "max_tokens": max_tokens}, timeout=timeout, retries=0)["choices"][0]["message"]
    return (msg.get("content") or "").strip()
