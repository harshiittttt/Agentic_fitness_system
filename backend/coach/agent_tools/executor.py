"""call_tool(name, args): validate against the schema, apply the risk tier IN CODE, dispatch in-process to backend.main.app.

- never a network call: httpx.ASGITransport on the app object, fixed base URL; arguments can only fill declared schema fields
  (unknown keys, hosts, URLs, headers are rejected), path values are percent-encoded
- the instance's API token (env API_TOKEN) is read here, put in the Authorization header here, and appears in nothing returned or logged
- 10 s per call, responses are capped to a parseable summary, errors come back as {ok:false,error}
- CONFIRM: nothing runs; the call is stored in voice_control's pending-confirm slot and {needs_confirm:true,say} returned
"""
from __future__ import annotations

import asyncio
import concurrent.futures as cf
import contextlib
import json
import os
import re
import time
from urllib.parse import quote

from . import catalog, risk

CALL_TIMEOUT = 10.0
MAX_RESULT_CHARS = 3500
_BASE = "http://voice-agent.local"            # never resolved: ASGITransport hands the request straight to the app
_POOL = cf.ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent-tool")
_TYPES = {"string": str, "boolean": bool, "array": list, "object": dict}


def _secret_values() -> list[str]:
    out = []
    for k, v in os.environ.items():
        if v and len(v) >= 8 and re.search(r"(TOKEN|KEY|SECRET|PASSWORD)", k.upper()):
            out.append(v)
    return out


def scrub(text) -> str:
    """Remove every secret-looking value (env values of TOKEN/KEY/SECRET vars, bearer tokens, API-key shapes) from text."""
    s = text if isinstance(text, str) else json.dumps(text, default=str, ensure_ascii=False)
    for v in _secret_values():
        s = s.replace(v, "[redacted]")
    s = re.sub(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}", "Bearer [redacted]", s)
    return re.sub(r"(AIza[0-9A-Za-z_\-]{10,}|sk-[A-Za-z0-9_\-]{10,})", "[redacted]", s)


# ------------------------------------------------------------------ validation
def validate(spec: dict, args) -> tuple[str | None, dict]:
    """-> (error or None, cleaned args)."""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return "args must be an object {name: value}", {}
    props = spec["schema"]["properties"]
    unknown = [k for k in args if k not in props]
    if unknown:
        return f"unknown argument(s) {unknown[:4]}; allowed: {sorted(props)[:12]}", {}
    missing = [k for k in spec["schema"].get("required", []) if k not in args or args[k] in (None, "")]
    if missing:
        return f"missing required argument(s): {missing}", {}
    out = {}
    for k, v in args.items():
        if v is None:
            continue
        s = props[k]
        t = s.get("type")
        if t in ("integer", "number"):
            if isinstance(v, str):
                try:
                    v = float(v) if t == "number" else int(float(v))
                except ValueError:
                    return f"{k} must be a {t}", {}
            if isinstance(v, bool) or not isinstance(v, (int, float)) or (t == "integer" and v != int(v)):
                return f"{k} must be a {t}", {}
            v = int(v) if t == "integer" else v
            if ("minimum" in s and v < s["minimum"]) or ("maximum" in s and v > s["maximum"]):
                return f"{k} out of range ({s.get('minimum')}..{s.get('maximum')})", {}
        elif t in _TYPES:
            if not isinstance(v, _TYPES[t]):
                if t == "string" and isinstance(v, (int, float)) and not isinstance(v, bool):
                    v = str(v)
                else:
                    return f"{k} must be a {t}", {}
            if t == "string" and len(v) > 2000:
                return f"{k} is too long", {}
        if s.get("pattern") and isinstance(v, str) and not re.search(s["pattern"], v):
            return f"{k} has the wrong format", {}
        if s.get("enum") and v not in s["enum"]:
            return f"{k} must be one of {s['enum'][:10]}", {}
        out[k] = v
    return None, out


# ------------------------------------------------------------------ result shrinking
def shrink(data, limit: int = MAX_RESULT_CHARS):
    """`data` itself when small, else a smaller structure that is still valid JSON (lists cut, long strings clipped) flagged truncated."""
    def dump(o):
        return json.dumps(o, default=str, ensure_ascii=False)
    if len(dump(data)) <= limit:
        return data

    def cut(o, items, chars):
        if isinstance(o, str):
            return o if len(o) <= chars else o[:chars] + "..."
        if isinstance(o, list):
            r = [cut(x, items, chars) for x in o[:items]]
            return r + [f"... {len(o) - items} more"] if len(o) > items else r
        if isinstance(o, dict):
            return {str(k): cut(v, items, chars) for k, v in list(o.items())[: max(items * 3, 12)]}
        return o
    for items, chars in ((12, 200), (6, 120), (3, 80), (2, 40), (1, 20)):
        r = cut(data, items, chars)
        if len(dump(r)) <= limit:
            return {"truncated": True, "data": r}
    return {"truncated": True, "data": dump(data)[: limit // 2]}


# ------------------------------------------------------------------ dispatch
def _build_request(spec: dict, args: dict):
    path = spec["path"]
    query, body = {}, {}
    for k, v in args.items():
        loc = spec["where"].get(k)
        if loc == "path":
            path = path.replace("{" + k + "}", quote(str(v), safe=""))
        elif loc == "query":
            query[k] = v
        elif k == "body" and spec["body_is_raw"]:
            body = v
        else:
            body[spec["orig"].get(k, k)] = v
    has_body = any(w == "body" for w in spec["where"].values())
    return spec["method"], path, query, (body if has_body else None)


async def _asgi(method: str, path: str, query: dict, body, token: str):
    import httpx
    from backend.main import app
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=_BASE, timeout=CALL_TIMEOUT) as c:
        return await c.request(method, path, params=query or None, json=body, headers=headers)


def _dispatch(spec: dict, args: dict):
    method, path, query, body = _build_request(spec, args)
    from backend.auth import api_token
    token = api_token()
    fut = _POOL.submit(lambda: asyncio.run(asyncio.wait_for(_asgi(method, path, query, body, token), CALL_TIMEOUT)))
    r = fut.result(timeout=CALL_TIMEOUT + 1)
    ctype = r.headers.get("content-type", "")
    if "json" in ctype:
        try:
            return r.status_code, r.json()
        except ValueError:
            pass
    if ctype.startswith(("audio/", "image/", "video/", "application/octet-stream", "application/zip", "application/pdf")):
        return r.status_code, {"note": "binary response omitted"}
    return r.status_code, {"text": r.text[:1000]}


def _brief(args: dict) -> str:
    return ", ".join(f"{k} {str(v)[:30]}" for k, v in list(args.items())[:3])


def _confirm_say(spec: dict, args: dict) -> str:
    base = risk.SAY.get((spec["method"], spec["path"])) or (f"That will {spec['summary'].lower().rstrip('.')}" + (f" ({_brief(args)})" if args else "") + ".")
    return f"{base} Say yes to confirm."


def kill_switch_on() -> bool:
    try:
        from fitness_os import ledger
        return bool(ledger.kill_switch())
    except Exception:
        return False


class _NoSpan:
    def set(self, *a, **k):
        pass


def _trace(name: str, tier: str):
    try:
        from fitness_os import trace
        return trace.span(name, kind="tool", tool=name, tier=tier)
    except Exception:
        return contextlib.nullcontext(_NoSpan())


def execute(spec: dict, args: dict) -> dict:
    """Run one validated call. Always returns {ok, ...}; never raises."""
    t0 = time.perf_counter()
    with _trace(spec["name"], spec["risk"]) as sp:
        try:
            code, data = _dispatch(spec, args)
        except Exception as e:      # timeout, app crash: the model gets a structured error
            kind = "timed out" if isinstance(e, (cf.TimeoutError, asyncio.TimeoutError, TimeoutError)) else type(e).__name__
            sp.set(outcome="error", error=kind)
            return {"ok": False, "error": f"{spec['name']} failed ({kind})", "ms": round((time.perf_counter() - t0) * 1000)}
        ms = round((time.perf_counter() - t0) * 1000)
        if code >= 400:
            detail = data.get("detail", data) if isinstance(data, dict) else data
            sp.set(outcome="error", error=f"http {code}", http=str(code))
            return {"ok": False, "status": code, "error": scrub(detail if isinstance(detail, str) else json.dumps(shrink(detail, 600), default=str))[:500], "ms": ms}
        sp.set(http=str(code))
        return {"ok": True, "status": code, "data": json.loads(scrub(shrink(data))), "ms": ms}


def run_confirmed(tool: str, args: dict) -> dict:
    """Executed by voice_control after a spoken yes. -> {status: applied|invalid, summary|error}"""
    spec = catalog.get_catalog().get(tool)
    if not spec or spec["risk"] == risk.BLOCKED:
        return {"status": "invalid", "error": "That action is no longer available."}
    err, clean = validate(spec, args)
    if err:
        return {"status": "invalid", "error": err}
    if spec["agent_run"] and kill_switch_on():
        return {"status": "invalid", "error": "The coaches are switched off (kill switch), so I can't start an agent run."}
    r = execute(spec, clean)
    if not r["ok"]:
        return {"status": "invalid", "error": r["error"]}
    return {"status": "applied", "summary": f"{spec['summary'].rstrip('.')}: done."}


def call_tool(name: str, args=None) -> dict:
    """The agent-facing entry. Returns {ok:true,data}, {ok:false,error}, or {ok:false,needs_confirm:true,say}.
    The loop reads `tier` and `needs_confirm`; the model only ever sees a scrubbed version."""
    spec = catalog.get_catalog().get(name)
    if not spec:
        from . import search
        hint = [h["name"] for h in search.find_tools(str(name), 3)]
        return {"ok": False, "error": f"unknown tool '{str(name)[:60]}'. Use find_tools first." + (f" Similar: {hint}" if hint else ""), "tier": None}
    tier = spec["risk"]
    if tier == risk.BLOCKED:
        return {"ok": False, "error": f"'{name}' is not allowed for the voice agent.", "tier": tier}
    err, clean = validate(spec, args)
    if err:
        return {"ok": False, "error": err, "tier": tier, "usage": catalog.compact_args(spec)}
    if spec["agent_run"] and kill_switch_on():
        return {"ok": False, "error": "The agent kill switch is ON, so I must not start an agent run. Tell the user to switch it off in the Team screen.", "tier": tier, "kill_switch": True}
    if tier == risk.CONFIRM:
        say = _confirm_say(spec, clean)
        from backend.coach import voice_control as vc
        vc._EXEC["agent_call"] = run_confirmed
        vc._ask_confirm("agent_call", {"tool": name, "args": clean}, say, "agent")
        return {"ok": False, "needs_confirm": True, "say": say, "tier": tier}
    r = execute(spec, clean)
    r["tier"] = tier
    return r
