"""Tests run fully offline: strip every LLM key so no test ever hits a real provider."""
import pytest

_KEYS = ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "LLM_API_KEY", "LLM_MAIN_API_KEY", "LLM_FAST_API_KEY", "LLM_CRITIC_API_KEY")


@pytest.fixture(autouse=True)
def _offline_llm(monkeypatch):
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)
