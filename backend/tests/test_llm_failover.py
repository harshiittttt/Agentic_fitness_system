"""A model whose free quota is spent is skipped at once; a second provider takes over; nothing waits on dead models."""
import json

import pytest

from backend.agents import base


class Resp:
    def __init__(self, status, payload):
        self.status_code, self._p, self.text = status, payload, json.dumps(payload)

    def json(self):
        return self._p


OK = {"choices": [{"message": {"content": "hi"}}], "usage": {}}
QUOTA = {"error": {"code": 429, "message": "quota", "details": [{"quotaId": "GenerateRequestsPerDayPerProject-FreeTier", "retryDelay": "66000s"}]}}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    base._COOLDOWN.clear()
    monkeypatch.setenv("GEMINI_API_KEY", "k1")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    for k in ("LLM2_BASE_URL", "LLM2_API_KEY", "LLM2_MODEL", "LLM2_FIRST_FOR"):
        monkeypatch.delenv(k, raising=False)
    yield
    base._COOLDOWN.clear()


def test_a_spent_daily_quota_is_skipped_on_every_later_call(monkeypatch):
    calls = []

    def post(url, json=None, **kw):
        calls.append(json["model"])
        return Resp(429, QUOTA) if json["model"] != "gemini-3.1-flash-lite" else Resp(200, OK)

    monkeypatch.setattr(base.httpx, "post", post)
    body = {"messages": [{"role": "user", "content": "x"}], "max_tokens": 5}
    assert base._post_raw("compose", body, 5, 0)["choices"][0]["message"]["content"] == "hi"
    first = list(calls)
    assert first[0] == "gemini-3.5-flash-lite" and first[-1] == "gemini-3.1-flash-lite"      # it walked to the working model
    calls.clear()
    base._post_raw("compose", body, 5, 0)
    assert calls == ["gemini-3.1-flash-lite"]                                               # and never touched the spent ones again
    assert "gemini-3.5-flash-lite" in base.cooldowns() and base.cooldowns()["gemini-3.5-flash-lite"] > 600
    assert "gemini-3.5-flash-lite" not in base.role_models("compose")


def test_a_second_provider_takes_over_when_the_first_is_out(monkeypatch):
    monkeypatch.setenv("LLM2_BASE_URL", "https://fast.example/v1")
    monkeypatch.setenv("LLM2_API_KEY", "k2")
    monkeypatch.setenv("LLM2_MODEL", "llama-fast")
    seen = []

    def post(url, json=None, headers=None, **kw):
        seen.append((url, json["model"], headers["Authorization"]))
        return Resp(429, QUOTA) if "generativelanguage" in url else Resp(200, OK)

    monkeypatch.setattr(base.httpx, "post", post)
    assert base.have_key("main")
    base._post_raw("main", {"messages": [{"role": "user", "content": "x"}], "max_tokens": 5}, 5, 0)
    assert seen[-1] == ("https://fast.example/v1/chat/completions", "llama-fast", "Bearer k2")


def test_the_second_provider_can_go_first_for_voice_roles(monkeypatch):
    monkeypatch.setenv("LLM2_BASE_URL", "https://fast.example/v1")
    monkeypatch.setenv("LLM2_API_KEY", "k2")
    monkeypatch.setenv("LLM2_MODEL", "llama-fast")
    monkeypatch.setenv("LLM2_FIRST_FOR", "compose,main")
    assert base.role_models("compose")[0] == "@2:llama-fast" and base.role_models("fast")[0] != "@2:llama-fast"


def test_it_works_with_only_the_second_provider_configured(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("LLM2_BASE_URL", "https://fast.example/v1")
    monkeypatch.setenv("LLM2_API_KEY", "k2")
    monkeypatch.setenv("LLM2_MODEL", "llama-fast")
    monkeypatch.setattr(base.httpx, "post", lambda url, json=None, **kw: Resp(200, OK))
    assert base.have_key("main") and base.role_models("main") == ["@2:llama-fast"]
    assert base._post_raw("main", {"messages": [], "max_tokens": 5}, 5, 0)["choices"][0]["message"]["content"] == "hi"


def test_diag_llm_reports_cooldowns():
    from fastapi.testclient import TestClient
    from backend.main import app
    base._cool_down("gemini-3.5-flash-lite", json.dumps(QUOTA))
    j = TestClient(app).get("/diag/llm").json()
    assert "gemini-3.5-flash-lite" in j["cooldown_seconds"] and "gemini-3.5-flash-lite" not in j["main"] and j["second_provider"] is False
