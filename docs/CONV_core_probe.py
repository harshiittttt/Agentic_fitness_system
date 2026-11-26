"""Scratch probe for the Conversation Core: which Gemini models exist for this key, time to first token with / without tools, how streamed tool
calls look on the OpenAI-compatible endpoint. Never prints the key.   python docs/CONV_core_probe.py"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import httpx  # noqa: E402

from backend.agents import base  # noqa: E402

c = base._cfg("compose")
KEY, URL = c["api_key"], c["base_url"]
print("have key:", bool(KEY), "| base:", URL, "| configured:", c["model"], c["fallbacks"], "| reasoning:", c["reasoning"] or "-")
H = {"Authorization": f"Bearer {KEY}"}

r = httpx.get(f"{URL}/models", headers=H, timeout=15)
print("models:", r.status_code)
ids = []
if r.status_code == 200:
    ids = sorted(m["id"].replace("models/", "") for m in r.json().get("data", []))
    print([i for i in ids if "flash" in i or "pro" in i and "tts" not in i][:40])

TOOLS = [{"type": "function", "function": {"name": "log_set", "description": "Save a set the athlete just did.",
          "parameters": {"type": "object", "properties": {"weight": {"type": "number"}, "reps": {"type": "integer"}, "reaction": {"type": "string"}}, "required": ["weight", "reps"]}}}]
SYS = "You are a gym coach speaking out loud. One short sentence. Use log_set when the athlete reports a set, with a short reaction."
MSGS = [{"role": "system", "content": SYS}, {"role": "user", "content": "sixty kilos five reps done"}]


def run(model, tools, reasoning=None, msgs=MSGS):
    body = {"model": model, "messages": msgs, "stream": True, "max_tokens": 200}
    if tools:
        body["tools"] = TOOLS
    if reasoning:
        body["reasoning_effort"] = reasoning
    t0 = time.time()
    first = None
    text, calls, n = "", [], 0
    try:
        with httpx.stream("POST", f"{URL}/chat/completions", json=body, headers=H, timeout=20) as s:
            if s.status_code != 200:
                s.read()
                return f"HTTP {s.status_code}: {s.text[:160]}"
            for line in s.iter_lines():
                if not line.startswith("data:"):
                    continue
                d = line[5:].strip()
                if d == "[DONE]":
                    break
                n += 1
                ch = json.loads(d)["choices"][0]
                dl = ch.get("delta", {})
                if (dl.get("content") or dl.get("tool_calls")) and first is None:
                    first = time.time() - t0
                text += dl.get("content") or ""
                if dl.get("tool_calls"):
                    calls.append(dl["tool_calls"])
    except Exception as e:
        return f"{type(e).__name__}: {e}"
    return f"first={first and round(first, 2)}s total={round(time.time() - t0, 2)}s chunks={n} text={text[:60]!r} calls={json.dumps(calls)[:300]}"


for m in ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.5-flash", "gemini-2.5-flash", "gemini-2.5-flash-lite"):
    if ids and m not in ids:
        print(m, "-> not in the model list")
        continue
    for tools in (False, True):
        for reasoning in (None, "low", "minimal", "none"):
            print(m, "tools" if tools else "plain", reasoning or "default", "->", run(m, tools, reasoning))
