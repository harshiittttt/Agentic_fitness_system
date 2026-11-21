"""Per-agent TTS voices: mock transport only, no network, no credits."""
import httpx
import pytest
from fastapi.testclient import TestClient

from backend.coach import voice
from backend.main import app
from backend.memory import db
from backend.tests.test_eleven import Mock


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    for k in ("ELEVEN_ENABLED", "ELEVEN_MONTHLY_CREDITS", "ELEVEN_CREDITS_PER_CHAR", "ELEVEN_VOICE_HYPE", "ELEVEN_VOICE_MENTOR",
              "ELEVEN_VOICE_STRAIGHT", "ELEVEN_AGENT_MAX_CHARS", "ELEVEN_AGENT_BUDGET_SHARE", "ELEVEN_VOICE_AGENT_NUTRITION"):
        monkeypatch.delenv(k, raising=False)
    mock = Mock()
    monkeypatch.setattr(voice, "_http", lambda: httpx.Client(transport=httpx.MockTransport(mock.handler)))
    voice.reset()
    yield mock
    voice.reset()


def test_every_specialist_has_a_voice_and_settings_differ():
    for a in ("training", "nutrition", "recovery", "progress", "periodization", "logistics", "intake", "research", "critic"):
        assert a in voice.AGENT_VOICES
    assert voice.AGENT_VOICES["training"][1] != voice.AGENT_VOICES["critic"][1]


def test_agent_uses_its_settings_and_is_cached_per_agent(env):
    a = voice.synthesize("Add one set.", "hype", agent="training")
    assert a.path and env.last["body"]["voice_settings"]["stability"] == voice.AGENT_VOICES["training"][1]["stability"]
    assert voice.synthesize("Add one set.", "hype", agent="training").cached
    calls = env.calls
    b = voice.synthesize("Add one set.", "hype", agent="critic")
    assert b.path != a.path and env.calls == calls + 1


def test_long_text_is_trimmed_not_refused(env):
    long = "First point here. " + "word " * 200
    r = voice.synthesize(long, "hype", agent="nutrition")
    assert r.path and len(env.last["body"]["text"]) <= 240


def test_env_voice_override_per_agent(env, monkeypatch):
    monkeypatch.setenv("ELEVEN_VOICE_AGENT_NUTRITION", "my_nut")
    voice.synthesize("Eat protein.", "hype", agent="nutrition")
    assert env.last["path"].endswith("/text-to-speech/my_nut")


def test_agent_budget_share_gives_text_only(env, monkeypatch):
    monkeypatch.setenv("ELEVEN_MONTHLY_CREDITS", "100")
    monkeypatch.setenv("ELEVEN_AGENT_BUDGET_SHARE", "0.2")
    r = voice.synthesize("This line is more than twenty chars.", "hype", agent="recovery")
    assert r.path is None and r.reason == "agent_budget" and env.calls == 0
    assert voice.synthesize("This line is more than twenty chars.", "hype").path   # coach voice still has budget


def test_unconfigured_falls_back_without_network(env, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY")
    r = voice.synthesize("Hello.", "hype", agent="training")
    assert r.path is None and r.reason == "not_configured" and env.calls == 0


def test_endpoint_accepts_agent_and_falls_back(env, monkeypatch):
    c = TestClient(app)
    r = c.get("/tts", params={"text": "Hi there.", "persona": "hype", "agent": "research"})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg"
    monkeypatch.delenv("ELEVENLABS_API_KEY")
    r = c.get("/tts", params={"text": "Hi there.", "agent": "research"})
    assert r.status_code == 503 and r.json()["fallback"] is True
