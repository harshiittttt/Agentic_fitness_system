"""A 200 response with no text and no tool call is a failed attempt: fall through to the next model, raise only when every model fails."""
import pytest

from backend.agents import base


class _R:
    def __init__(self, data, status=200):
        self._d, self.status_code, self.text = data, status, str(data)

    def json(self):
        return self._d


def _ok(text=None, tool_calls=None):
    return {"choices": [{"message": {"content": text, "tool_calls": tool_calls}}], "usage": {"prompt_tokens": 5, "completion_tokens": 1}}


@pytest.fixture(autouse=True)
def cfg(monkeypatch):
    monkeypatch.setattr(base, "_cfg", lambda role: {"api_key": "k", "base_url": "http://x", "model": "m1", "fallbacks": ["m2", "m3"], "reasoning": ""})
    monkeypatch.setattr(base.time, "sleep", lambda s: None)


def _script(monkeypatch, replies):
    calls = []

    def post(url, json=None, **kw):
        calls.append(json["model"])
        return replies[json["model"]]
    monkeypatch.setattr(base.httpx, "post", post)
    return calls


def test_empty_reply_falls_through_to_next_model_without_retrying_the_same_one(monkeypatch):
    calls = _script(monkeypatch, {"m1": _R(_ok(None)), "m2": _R(_ok("   ")), "m3": _R(_ok("hello"))})
    out = base._post_raw("main", {"messages": []}, retries=1)
    assert out["choices"][0]["message"]["content"] == "hello"
    assert calls == ["m1", "m2", "m3"]          # one attempt each: an empty reply is not retried


def test_tool_call_without_text_is_a_valid_reply(monkeypatch):
    calls = _script(monkeypatch, {"m1": _R(_ok(None, tool_calls=[{"id": "1", "function": {"name": "t", "arguments": "{}"}}]))})
    out = base._post_raw("main", {"messages": []})
    assert out["choices"][0]["message"]["tool_calls"] and calls == ["m1"]


def test_all_models_empty_raises_llmerror(monkeypatch):
    _script(monkeypatch, {m: _R(_ok(None)) for m in ("m1", "m2", "m3")})
    with pytest.raises(base.LLMError) as e:
        base._post_raw("main", {"messages": []})
    assert "empty reply" in str(e.value)


def test_response_without_choices_is_also_unusable(monkeypatch):
    calls = _script(monkeypatch, {"m1": _R({"weird": True}), "m2": _R(_ok("fine"))})
    out = base._post_raw("main", {"messages": []})
    assert out["choices"][0]["message"]["content"] == "fine" and calls == ["m1", "m2"]
