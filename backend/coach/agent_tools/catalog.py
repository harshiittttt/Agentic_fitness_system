"""Tool catalog generated from the app's own OpenAPI schema: one spec per FastAPI operation (no hand-written tools).

Spec: {name, method, path, summary, description, tags, risk, agent_run, schema (flat JSON schema of ALL args), where {arg: path|query|body}, body_is_raw}
Built once per process (lazily) from backend.main.app.openapi().
"""
from __future__ import annotations

import re
import threading

from . import risk

_lock = threading.Lock()
_CAT: dict[str, dict] | None = None
METHODS = ("get", "post", "put", "patch", "delete")


def _resolve(schema, comps, depth=0):
    if not isinstance(schema, dict) or depth > 6:
        return schema if isinstance(schema, dict) else {}
    if "$ref" in schema:
        return _resolve(comps.get(schema["$ref"].split("/")[-1], {}), comps, depth + 1)
    out = dict(schema)
    for k in ("anyOf", "oneOf"):
        if k in out:                                   # Optional[X] -> X
            opts = [o for o in out.pop(k) if not (isinstance(o, dict) and o.get("type") == "null")]
            if opts:
                out = {**_resolve(opts[0], comps, depth + 1), **{kk: vv for kk, vv in out.items() if kk in ("description", "default", "title")}}
    if "allOf" in out:
        for o in out.pop("allOf"):
            out = {**_resolve(o, comps, depth + 1), **out}
    if isinstance(out.get("properties"), dict):
        out["properties"] = {k: _resolve(v, comps, depth + 1) for k, v in out["properties"].items()}
    if isinstance(out.get("items"), dict):
        out["items"] = _resolve(out["items"], comps, depth + 1)
    return out


def _func_name(op_id: str, method: str, path: str) -> str:
    slug = re.sub(r"\W", "_", path) + "_" + method
    return op_id[: -len(slug)].rstrip("_") if op_id.endswith(slug) else op_id


def build_catalog(app=None) -> dict[str, dict]:
    if app is None:
        from backend.main import app as _app
        app = _app
    spec = app.openapi()
    comps = spec.get("components", {}).get("schemas", {})
    cat: dict[str, dict] = {}
    for path, item in spec.get("paths", {}).items():
        for method in METHODS:
            op = item.get(method)
            if not op:
                continue
            M = method.upper()
            props: dict = {}
            required: list = []
            where: dict = {}
            orig: dict = {}      # arg name -> original body key (when it was renamed)
            for p in op.get("parameters", []):
                if p.get("in") not in ("path", "query"):
                    continue
                s = _resolve(p.get("schema", {}), comps)
                s = {k: v for k, v in s.items() if k in ("type", "enum", "minimum", "maximum", "items", "format", "default", "minLength", "maxLength", "pattern")}
                if p.get("description"):
                    s["description"] = p["description"]
                props[p["name"]] = s
                where[p["name"]] = p["in"]
                if p.get("required"):
                    required.append(p["name"])
            raw_body = False
            rb = op.get("requestBody")
            if rb:
                content = rb.get("content", {})
                js = content.get("application/json")
                if js is None:      # multipart / octet-stream: an upload, never exposed
                    raw_body = None
                else:
                    b = _resolve(js.get("schema", {}), comps)
                    if isinstance(b.get("properties"), dict) and b["properties"]:
                        for k, v in b["properties"].items():
                            name = k if k not in props else f"body_{k}"
                            props[name] = v
                            where[name] = "body"
                            orig[name] = k
                        for k in b.get("required", []):
                            required.append(k if k in props else f"body_{k}")
                    else:
                        props["body"] = {**{k: v for k, v in b.items() if k in ("type", "items", "description")}, "type": b.get("type", "object")}
                        where["body"] = "body"
                        raw_body = True
                        if rb.get("required"):
                            required.append("body")
            tier = risk.tier_for(M, path)
            if raw_body is None:
                tier = risk.BLOCKED
            fn = _func_name(op.get("operationId", ""), method, path)
            seg = path.strip("/").split("/")[0].replace("-", "_")
            fn = fn if (fn.startswith(seg) or not seg) else f"{seg}_{fn}"
            name = fn if fn and fn not in cat else f"{method}_{re.sub(r'[^a-z0-9]+', '_', path.lower()).strip('_')}"
            desc = " ".join((op.get("description") or op.get("summary") or fn.replace("_", " ")).split())
            cat[name] = {"name": name, "method": M, "path": path, "summary": (op.get("summary") or fn.replace("_", " ")).strip(), "description": desc[:400],
                         "tags": op.get("tags", []), "risk": tier, "agent_run": risk.is_agent_run(M, path),
                         "schema": {"type": "object", "properties": props, "required": sorted(set(required)), "additionalProperties": False},
                         "where": where, "orig": orig, "body_is_raw": bool(raw_body), "source": "generated"}
    return cat


def get_catalog(refresh: bool = False) -> dict[str, dict]:
    global _CAT
    with _lock:
        if _CAT is None or refresh:
            _CAT = build_catalog()
        return _CAT


def callable_tools() -> list[dict]:
    return [t for t in get_catalog().values() if t["risk"] != risk.BLOCKED]


def compact_args(spec: dict) -> dict:
    """The argument hint a model needs: {arg: type[/enum], required marked with *}."""
    out = {}
    req = set(spec["schema"].get("required", []))
    for k, v in spec["schema"]["properties"].items():
        t = v.get("type", "any")
        if v.get("enum"):
            t = "|".join(map(str, v["enum"][:8]))
        out[k + ("*" if k in req else "")] = t
    return out
