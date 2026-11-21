"""ElevenLabs integration, tested against a mock server: no network, no credits spent."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.coach import voice
from backend.main import app
from backend.memory import db
from backend.tools.demo import PROFILE

VOICES = {"voices": [
    {"voice_id": "v_rachel", "name": "Rachel", "category": "premade"},
    {"voice_id": "v_josh", "name": "Josh", "category": "premade"},
    {"voice_id": "v_daniel", "name": "Daniel - Steady Broadcaster", "category": "premade"},
    {"voice_id": "v_cloned", "name": "My Clone", "category": "cloned"},
]}


class Mock:
    """Stands in for api.elevenlabs.io and records what was asked."""
    def __init__(self, tts_status=200):
        self.tts_status, self.calls, self.voice_calls, self.last = tts_status, 0, 0, None

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/voices"):
            self.voice_calls += 1
            return httpx.Response(200, json=VOICES)
        self.calls += 1
        self.last = {"path": request.url.path, "key": request.headers.get("xi-api-key"), "body": json.loads(request.content),
                     "fmt": request.url.params.get("output_format")}
        return httpx.Response(self.tts_status, content=b"ID3fake-mp3" if self.tts_status == 200 else b"")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    for k in ("ELEVEN_ENABLED", "ELEVEN_MONTHLY_CREDITS", "ELEVEN_CREDITS_PER_CHAR", "ELEVEN_MODEL",
              "ELEVEN_VOICE_HYPE", "ELEVEN_VOICE_MENTOR", "ELEVEN_VOICE_STRAIGHT"):
        monkeypatch.delenv(k, raising=False)
    mock = Mock()
    monkeypatch.setattr(voice, "_http", lambda: httpx.Client(transport=httpx.MockTransport(mock.handler)))
    voice.reset()
    yield mock
    voice.reset()


def test_not_configured_makes_no_network_calls(env, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY")
    r = voice.synthesize("Nice set.", "hype")
    assert r.path is None and r.reason == "not_configured" and env.calls == 0 and env.voice_calls == 0


def test_synthesis_sends_the_right_request_and_records_usage(env):
    r = voice.synthesize("Solid set! 70 for 6.", "hype")
    assert r.path and r.reason is None and not r.cached
    assert open(r.path, "rb").read() == b"ID3fake-mp3"
    assert env.last["key"] == "test-key" and env.last["path"].endswith("/text-to-speech/v_josh")
    assert env.last["body"]["text"] == "Solid set! 70 for 6." and env.last["body"]["model_id"] == "eleven_flash_v2_5"
    assert env.last["fmt"] == "mp3_44100_64"
    assert db.tts_usage(db.now()[:7]) == len("Solid set! 70 for 6.")   # 1 credit/char by default (conservative)


def test_repeated_lines_come_from_the_cache_and_cost_nothing(env):
    a = voice.synthesize("Rest two minutes.", "mentor")
    used = db.tts_usage(db.now()[:7])
    b = voice.synthesize("  Rest   two minutes. ", "mentor")      # same text, messy whitespace
    assert b.cached and b.path == a.path and env.calls == 1 and db.tts_usage(db.now()[:7]) == used


def test_cache_is_per_voice(env):
    a, b = voice.synthesize("Let's go.", "hype"), voice.synthesize("Let's go.", "mentor")
    assert a.path != b.path and env.calls == 2


def test_voice_selection(env, monkeypatch):
    assert voice.voice_for("hype") == ("v_josh", "Josh")
    assert voice.voice_for("mentor") == ("v_rachel", "Rachel")
    assert voice.voice_for("straight") == ("v_daniel", "Daniel - Steady Broadcaster")   # "Name - description" still matches
    monkeypatch.setenv("ELEVEN_VOICE_HYPE", "my_voice_id")
    assert voice.voice_for("hype") == ("my_voice_id", "custom")
    assert env.voice_calls == 1                                                          # voice list fetched once, then cached


def test_falls_back_to_first_premade_voice(env, monkeypatch):
    monkeypatch.setattr(voice, "PREFERRED", {"hype": ["Nobody"]})
    assert voice.voice_for("hype")[0] == "v_rachel"


def test_monthly_budget_is_a_hard_cap(env, monkeypatch):
    monkeypatch.setenv("ELEVEN_MONTHLY_CREDITS", "30")
    assert voice.synthesize("Twenty-five chars here.", "hype").path       # 23 chars, fits
    r = voice.synthesize("Another line that will not fit.", "hype")
    assert r.path is None and r.reason == "budget" and env.calls == 1


def test_cheaper_model_rate_is_configurable(env, monkeypatch):
    monkeypatch.setenv("ELEVEN_CREDITS_PER_CHAR", "0.5")
    voice.synthesize("Ten chars!!", "hype")                                  # 11 chars
    assert db.tts_usage(db.now()[:7]) == 5.5


def test_usage_is_tracked_per_month(env):
    db.add_tts_usage("2020-01", 999)
    assert db.tts_usage(db.now()[:7]) == 0 and db.tts_usage("2020-01") == 999


@pytest.mark.parametrize("status,reason", [(401, "auth"), (429, "quota"), (402, "quota"), (500, "error")])
def test_api_errors_map_to_reasons_and_never_cache_or_charge(env, status, reason):
    env.tts_status = status
    r = voice.synthesize("Hello there.", "hype")
    assert r.path is None and r.reason == reason and db.tts_usage(db.now()[:7]) == 0


def test_auth_failure_triggers_a_cooldown_instead_of_hammering_the_api(env):
    env.tts_status = 401
    voice.synthesize("One.", "hype")
    calls = env.calls
    assert voice.synthesize("Two.", "hype").reason == "cooldown" and env.calls == calls


def test_bad_inputs(env):
    assert voice.synthesize("   ", "hype").reason == "empty"
    assert voice.synthesize("x" * 401, "hype").reason == "too_long" and env.calls == 0


def test_disable_switch(env, monkeypatch):
    monkeypatch.setenv("ELEVEN_ENABLED", "0")
    assert voice.synthesize("Hi.", "hype").reason == "not_configured"


# ---------------- HTTP endpoints ----------------
def test_tts_endpoint_streams_mp3(env):
    c = TestClient(app)
    r = c.get("/tts", params={"text": "Nice work.", "persona": "straight"})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg" and r.content == b"ID3fake-mp3"
    assert r.headers["x-voice-cached"] == "0"
    assert c.get("/tts", params={"text": "Nice work.", "persona": "straight"}).headers["x-voice-cached"] == "1"
    assert env.last["path"].endswith("/v_daniel")


def test_tts_endpoint_unknown_persona_uses_default(env):
    assert TestClient(app).get("/tts", params={"text": "Hi.", "persona": "robot"}).status_code == 200
    assert env.last["path"].endswith("/v_josh")


def test_tts_endpoint_tells_the_app_to_fall_back(env, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY")
    r = TestClient(app).get("/tts", params={"text": "Hi."})
    assert r.status_code == 503 and r.json() == {"fallback": True, "reason": "not_configured", "detail": ""}


def test_status_reports_budget_and_optionally_probes(env):
    c = TestClient(app)
    voice.synthesize("Twenty chars exactly", "hype")
    s = c.get("/tts/status").json()
    assert s["configured"] and s["used"] == 20 and s["remaining"] == 8980 and s["cached_lines"] == 1 and s["voices"] == {}
    p = c.get("/tts/status", params={"probe": True}).json()
    assert p["ok"] and p["voices"]["hype"] == "Josh" and p["voices"]["mentor"] == "Rachel"


def test_status_probe_reports_a_bad_key(env):
    env.tts_status = 200
    voice.reset()
    import backend.coach.voice as v
    v._http = lambda: httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(401, json={"detail": "bad key"})))
    p = TestClient(app).get("/tts/status", params={"probe": True}).json()
    assert p["ok"] is False and p["error"] == "auth"


# ---------------- which lines deserve the premium voice ----------------
def test_live_replies_flag_key_moments(env):
    c = TestClient(app)
    c.post("/onboard", json={**PROFILE, "name": "Alex", "coach_persona": "hype"})
    assert c.post("/live/start").json()["moment"] is True
    ordinary = c.post("/live/say", json={"transcript": "bench press 70 for 6 rpe 8"}).json()
    assert ordinary["intent"] == "set" and ordinary["moment"] is False
    assert c.post("/live/say", json={"transcript": "my elbow hurts"}).json()["moment"] is True
    assert c.post("/live/say", json={"transcript": "I only have 25 minutes"}).json()["moment"] is True
    assert c.post("/live/say", json={"transcript": "wrap it up"}).json()["moment"] is True
    for i in range(3):
        db.add_set(f"2020-01-0{i + 1}", "squat", 100, 8, 8)
    assert c.post("/live/say", json={"transcript": "squat 130 for 8 rpe 8"}).json()["moment"] is True   # PR


# ---------------- restricted keys (the real-world case: key lacks voices_read) ----------------
class Restricted(Mock):
    """A restricted key: cannot list voices, can speak. Some voice ids are not on the account."""
    def __init__(self, missing_voices=(), **kw):
        super().__init__(**kw)
        self.missing, self.tried = set(missing_voices), []

    def handler(self, request):
        if request.url.path.endswith("/voices"):
            self.voice_calls += 1
            return httpx.Response(401, json={"detail": {"type": "authentication_error", "code": "unauthorized", "status": "missing_permissions",
                                                         "message": "The API key you used is missing the permission voices_read to execute this operation."}})
        vid = request.url.path.rsplit("/", 1)[-1]
        self.tried.append(vid)
        if vid in self.missing:
            return httpx.Response(404, json={"detail": {"status": "voice_not_found", "message": "A voice with the voice id was not found."}})
        return super().handler(request)


@pytest.fixture()
def restricted(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("ELEVENLABS_API_KEY", "restricted-key")
    for k in ("ELEVEN_ENABLED", "ELEVEN_MONTHLY_CREDITS", "ELEVEN_CREDITS_PER_CHAR", "ELEVEN_VOICE_HYPE"):
        monkeypatch.delenv(k, raising=False)
    box = {"mock": Restricted()}
    monkeypatch.setattr(voice, "_http", lambda: httpx.Client(transport=httpx.MockTransport(box["mock"].handler)))
    voice.reset()
    yield box
    voice.reset()


def test_restricted_key_still_speaks_using_builtin_voice_ids(restricted):
    r = voice.synthesize("Nice set!", "hype")
    assert r.path and r.reason is None and r.voice == "Liam"
    assert restricted["mock"].last["path"].endswith("/" + voice.FALLBACK_IDS["Liam"])


def test_unavailable_voice_falls_through_to_the_next_one(restricted):
    restricted["mock"] = Restricted(missing_voices=[voice.FALLBACK_IDS["Liam"], voice.FALLBACK_IDS["Charlie"]])
    r = voice.synthesize("Nice set!", "hype")
    assert r.path and r.voice == "Josh"
    assert restricted["mock"].tried == [voice.FALLBACK_IDS[n] for n in ("Liam", "Charlie", "Josh")]
    # the working voice is remembered, so the next new line goes straight to it
    voice.synthesize("Another line.", "hype")
    assert restricted["mock"].tried[-1] == voice.FALLBACK_IDS["Josh"] and len(restricted["mock"].tried) == 4


def test_every_voice_missing_reports_no_voice_and_charges_nothing(restricted):
    restricted["mock"] = Restricted(missing_voices=voice.FALLBACK_IDS.values())
    r = voice.synthesize("Hello.", "hype")
    assert r.path is None and r.reason == "no_voice" and db.tts_usage(db.now()[:7]) == 0


def test_missing_tts_permission_is_reported_clearly_with_elevenlabs_own_message(restricted, monkeypatch):
    def handler(request):
        return httpx.Response(401, json={"detail": {"status": "missing_permissions", "message": "The API key you used is missing the permission text_to_speech."}})
    restricted["mock"].handler = handler
    r = voice.synthesize("Hello.", "hype")
    assert r.reason == "permission" and "text_to_speech" in r.detail
    assert voice.synthesize("Again.", "hype").reason == "cooldown"        # and we stop asking for a while


def test_out_of_credits_returned_as_401_is_recognised_as_quota(restricted):
    restricted["mock"].handler = lambda request: httpx.Response(401, json={"detail": {"status": "quota_exceeded", "message": "You have 0 credits remaining."}})
    assert voice.synthesize("Hello.", "hype").reason == "quota"


def test_status_probe_flags_a_restricted_key(restricted):
    s = voice.status(probe=True)
    assert s["ok"] is True and s["limited_key"] is True and s["voices"]["hype"] == "Liam" and s["voices"]["straight"] == "Daniel"


def test_test_endpoint_reports_success_then_serves_from_cache(restricted):
    c = TestClient(app)
    first = c.get("/tts/test", params={"persona": "mentor"}).json()
    assert first["ok"] and first["voice"] == "Matilda" and first["credits"] > 0 and not first["cached"]
    again = c.get("/tts/test", params={"persona": "mentor"}).json()
    assert again["ok"] and again["cached"] and again["credits"] == 0 and again["used"] == first["used"]


def test_test_endpoint_explains_a_refusal(restricted):
    restricted["mock"].handler = lambda request: httpx.Response(401, json={"detail": {"status": "missing_permissions", "message": "missing text_to_speech"}})
    r = TestClient(app).get("/tts/test", params={"persona": "hype"}).json()
    assert r["ok"] is False and r["reason"] == "permission" and "text_to_speech" in r["detail"]


def test_library_voice_refusal_on_free_plan_means_try_the_next_voice_not_out_of_credits(restricted):
    """The exact error a free account gets for legacy premade voices (seen live): 402 payment_required."""
    blocked = {voice.FALLBACK_IDS["Liam"], voice.FALLBACK_IDS["Charlie"]}

    def handler(request):
        vid = request.url.path.rsplit("/", 1)[-1]
        restricted["mock"].tried.append(vid)
        if vid in blocked:
            return httpx.Response(402, json={"detail": {"status": "payment_required", "message": "Free users cannot use library voices via the API. Please upgrade your subscription to use this voice."}})
        return httpx.Response(200, content=b"ID3ok")
    restricted["mock"].handler = lambda r: handler(r) if not r.url.path.endswith("/voices") else Restricted.handler(restricted["mock"], r)
    r = voice.synthesize("Nice set!", "hype")
    assert r.path and r.voice == "Josh" and r.reason is None
    assert voice.synthesize("Next line.", "hype").reason is None          # not blocked in a cool-down: it was never a quota problem


def test_all_voices_library_only_gives_a_helpful_reason(restricted):
    msg = "Free users cannot use library voices via the API. Please upgrade your subscription to use this voice."
    restricted["mock"].handler = lambda r: httpx.Response(401, json={"detail": {"status": "missing_permissions", "message": "x"}}) if r.url.path.endswith("/voices")         else httpx.Response(402, json={"detail": {"status": "payment_required", "message": msg}})
    r = voice.synthesize("Hello.", "hype")
    assert r.path is None and r.reason == "no_voice" and "library voices" in r.detail
