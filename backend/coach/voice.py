"""ElevenLabs voice for the coach.

The API key stays on the backend; the app asks us for audio. Everything here is built around the FREE plan's small
monthly budget (about 10,000 credits): a disk cache so a repeated line costs nothing, a hard monthly cap, a cool-down
after auth/quota errors, and clean failure so the app can fall back to the phone's own voice.

Works with RESTRICTED keys too: if the key cannot list voices (missing the voices_read permission) we fall back to
well-known built-in voice IDs and just try them.

Env (all optional except the key):
  ELEVENLABS_API_KEY        the key (put it in .env, never in the app)
  ELEVEN_MODEL              default eleven_flash_v2_5 (fast, cheap)
  ELEVEN_CREDITS_PER_CHAR   default 1.0 (conservative; Flash may be 0.5, check your ElevenLabs usage page and lower it)
  ELEVEN_MONTHLY_CREDITS    default 9000 (free plan is 10,000; leaves a margin)
  ELEVEN_VOICE_HYPE / _MENTOR / _STRAIGHT / _HARYANVI / _ROAST   override a persona's voice with a voice_id
  ELEVEN_ENABLED=0          turn the integration off
  ELEVEN_VOICE_AGENT_<NAME> override one specialist's voice_id (e.g. ELEVEN_VOICE_AGENT_NUTRITION)
  ELEVEN_AGENT_MAX_CHARS    longest agent line spoken (default 240); longer text is trimmed at a sentence/word
  ELEVEN_AGENT_BUDGET_SHARE default 0.5: agent voices stop (text only) once this share of the monthly budget is used,
                            so the coach persona keeps the rest
"""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass

import httpx

from backend.memory import db

BASE = "https://api.elevenlabs.io/v1"
MAX_CHARS = 400
COOLDOWN_SECONDS = 600  # after an auth/quota/permission failure, stop asking for a while instead of failing slowly on every line

# Premade voice names to look for in the account, in order of preference. Library voices are NOT usable through the
# API on the free plan, so we stick to premade ones.
# Newer default voices (Liam, Charlie, Matilda, Sarah, George, Daniel, Roger) come first: the older "legacy" premade
# voices (Josh, Rachel, Adam...) are now classed as library voices, which free accounts cannot use through the API.
PREFERRED = {
    "hype": ["Liam", "Charlie", "Josh", "Adam", "Antoni"],
    "mentor": ["Matilda", "Sarah", "George", "Rachel", "Elli"],
    "straight": ["Daniel", "Roger", "Arnold", "Clyde", "Sam"],
    "haryanvi": ["Daniel", "Roger", "George", "Liam"],   # deeper, grounded voices suit a Haryanvi akhada coach
    "roast": ["Callum", "Harry", "Brian", "Roger", "Daniel"],   # Callum = husky trickster; all verified to accept Hindi on the free plan
}
# Languages we ask ElevenLabs to enforce (Flash/Turbo v2.5 support this). Haryanvi is not an official language, so we
# speak it as Hindi: the dialect is close and the Devanagari text reads naturally, but expect a non-native accent on free-plan voices.
LANG = {"haryanvi": "hi", "roast": "hi"}
# Well-known premade voice IDs, used when the key cannot list voices. If an ID is not on the account the API says
# voice_not_found and we simply try the next name.
FALLBACK_IDS = {
    "Josh": "TxGEqnHWrfWFTfGW9XjX", "Liam": "TX3LPaxmHKxFdv7VOQHJ", "Charlie": "IKne3meq5aSn9XLyUdCD",
    "Adam": "pNInz6obpgDQGcFmaJgB", "Antoni": "ErXwobaYiN019PkySvjV",
    "Matilda": "XrExE9yKIg1WjnnlVkGX", "Sarah": "EXAVITQu4vr4xnSDxMaL", "Rachel": "21m00Tcw4TiwG8CgLU8U",
    "George": "JBFqnCBsd6RMkjVDRZzb", "Elli": "MF3mGyEYCl7XYWbV9V6O",
    "Daniel": "onwK4e9ZLuTAKqWW03F9", "Arnold": "VR6AewLTigWG4xSOukaG", "Roger": "CwhRBWXzGAHq8TQ4Fs17",
    "Clyde": "2EiwWnXFnvU5JabPnv8n", "Sam": "yoZ06aMxZJJ28mfd3POQ",
    "Callum": "N2lVS1w4EtoT3dr4eOWO", "Harry": "SOYHLrjzK2X1ezoPC6cr", "Brian": "nPczCjzI2devNBz1zQrb",
}
# Livelier for Max, steadier for Sage, flatter for Ash.
SETTINGS = {
    "hype": {"stability": 0.35, "similarity_boost": 0.75, "style": 0.45},
    "mentor": {"stability": 0.60, "similarity_boost": 0.75, "style": 0.20},
    "straight": {"stability": 0.50, "similarity_boost": 0.75, "style": 0.25},
    "haryanvi": {"stability": 0.45, "similarity_boost": 0.75, "style": 0.35},
    "roast": {"stability": 0.30, "similarity_boost": 0.75, "style": 0.60},   # less stable + more style = more attitude
}
SAMPLES = {"hype": "Hey, I'm Max! I'll be in your ear the whole session.", "mentor": "Hi, I'm Sage. One set at a time.",
           "straight": "I'm Ash. Try to keep up.",
           "haryanvi": "राम राम भाई! मैं सूं तेरा पहलवान कोच।",
           "roast": "सुन, मैं तेरा उस्ताद हूँ। मुझे तेरी कोई परवाह नहीं है। बस सेट कर और बहाने बंद रख।"}


# Specialist agents reuse the three existing English voices (no new paid voices) and differ by voice settings.
# agent -> (base persona whose voice is used, voice_settings). Env ELEVEN_VOICE_AGENT_<NAME> swaps in a different voice_id.
AGENT_VOICES: dict[str, tuple[str, dict]] = {
    "training":      ("hype",     {"stability": 0.40, "similarity_boost": 0.75, "style": 0.40}),
    "nutrition":     ("mentor",   {"stability": 0.55, "similarity_boost": 0.75, "style": 0.30}),
    "recovery":      ("mentor",   {"stability": 0.75, "similarity_boost": 0.75, "style": 0.10}),
    "sleep":         ("mentor",   {"stability": 0.85, "similarity_boost": 0.75, "style": 0.05}),
    "injury":        ("straight", {"stability": 0.65, "similarity_boost": 0.75, "style": 0.15}),
    "progress":      ("hype",     {"stability": 0.55, "similarity_boost": 0.75, "style": 0.25}),
    "adherence":     ("hype",     {"stability": 0.30, "similarity_boost": 0.75, "style": 0.55}),
    "periodization": ("straight", {"stability": 0.50, "similarity_boost": 0.75, "style": 0.30}),
    "logistics":     ("straight", {"stability": 0.45, "similarity_boost": 0.75, "style": 0.40}),
    "intake":        ("mentor",   {"stability": 0.50, "similarity_boost": 0.75, "style": 0.35}),
    "research":      ("straight", {"stability": 0.70, "similarity_boost": 0.75, "style": 0.10}),
    "critic":        ("straight", {"stability": 0.80, "similarity_boost": 0.75, "style": 0.05}),
    "moderator":     ("mentor",   {"stability": 0.60, "similarity_boost": 0.75, "style": 0.20}),
}


def agent_max_chars() -> int:
    return min(MAX_CHARS, int(os.environ.get("ELEVEN_AGENT_MAX_CHARS", "240")))


def agent_budget_share() -> float:
    return float(os.environ.get("ELEVEN_AGENT_BUDGET_SHARE", "0.5"))


def trim_for_speech(text: str, limit: int) -> str:
    """Shorten to <= limit chars, preferring a sentence end, else a word boundary."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    ends = [cut.rfind(c) for c in ".!?"]
    i = max(ends)
    if i >= limit // 2:
        return cut[: i + 1]
    return cut.rsplit(" ", 1)[0].rstrip(",;:- ") + "."


def api_key() -> str:
    return os.environ.get("ELEVENLABS_API_KEY", "").strip()


def model() -> str:
    return os.environ.get("ELEVEN_MODEL", "eleven_flash_v2_5")


def credits_per_char() -> float:
    return float(os.environ.get("ELEVEN_CREDITS_PER_CHAR", "1.0"))


def budget() -> float:
    return float(os.environ.get("ELEVEN_MONTHLY_CREDITS", "9000"))


def configured() -> bool:
    return bool(api_key()) and os.environ.get("ELEVEN_ENABLED", "1") != "0"


@dataclass
class Result:
    path: str | None
    reason: str | None = None   # why there is no audio: not_configured, budget, auth, permission, quota, cooldown, too_long, empty, error...
    cached: bool = False
    credits: float = 0.0
    detail: str = ""            # ElevenLabs' own explanation when it refused (safe to show; contains no secrets)
    voice: str = ""             # display name of the voice used


_voices: list[dict] | None = None
_working: dict[str, tuple[str, str]] = {}   # persona -> (voice_id, name) that has actually worked
_blocked_until = 0.0
_blocked_reason: tuple[str, str] = ("", "")


def reset() -> None:
    """Forget cached voice lists, working voices and cool-downs (used by tests and when the key changes)."""
    global _voices, _blocked_until, _blocked_reason
    _voices, _blocked_until, _blocked_reason = None, 0.0, ("", "")
    _working.clear()


def _http() -> httpx.Client:  # tests replace this with a mock transport
    return httpx.Client(timeout=10.0)


def _headers() -> dict:
    return {"xi-api-key": api_key()}


def _err(resp: httpx.Response) -> tuple[str, str]:
    """(reason, detail) from an ElevenLabs error response. Handles their structured errors."""
    code, detail = "", ""
    try:
        d = resp.json().get("detail")
        if isinstance(d, dict):
            code, detail = d.get("status", ""), d.get("message", "")
    except Exception:
        pass
    if code == "missing_permissions":
        return "permission", detail
    if code in ("payment_required", "paid_plan_required") and "voice" in detail.lower():
        return "voice_unavailable", detail   # e.g. "Free users cannot use library voices via the API": just try another voice
    if code in ("quota_exceeded", "character_limit_exceeded") or resp.status_code in (402, 429):
        return "quota", detail
    if code in ("voice_not_found", "voice_not_fine_tuned") or resp.status_code == 404:
        return "voice_not_found", detail
    return {401: "auth", 403: "auth", 422: "bad_request"}.get(resp.status_code, "error"), detail


def list_voices() -> list[dict]:
    global _voices
    if _voices is None:
        with _http() as c:
            r = c.get(f"{BASE}/voices", headers=_headers())
            r.raise_for_status()
            _voices = r.json().get("voices", [])
    return _voices


def candidates(persona: str, agent: str = "") -> tuple[list[tuple[str, str]], bool]:
    """Voices to try for a persona, best first, and whether the key could list voices.
    Raises httpx.HTTPStatusError for real auth/quota problems."""
    override = (agent and os.environ.get(f"ELEVEN_VOICE_AGENT_{agent.upper()}")) or os.environ.get(f"ELEVEN_VOICE_{persona.upper()}")
    if override:
        return [(override, "custom")], True
    try:
        voices = list_voices()
    except httpx.HTTPStatusError as e:
        reason, _ = _err(e.response)
        if reason != "permission":
            raise
        return [(FALLBACK_IDS[n], n) for n in PREFERRED.get(persona, []) if n in FALLBACK_IDS], False   # restricted key
    by_name = {v.get("name", "").split(" - ")[0].strip().lower(): v for v in voices}  # newer names look like "Name - description"
    picked = [(by_name[n.lower()]["voice_id"], by_name[n.lower()]["name"]) for n in PREFERRED.get(persona, []) if n.lower() in by_name]
    pool = [v for v in voices if v.get("category") == "premade"] or voices
    picked += [(v["voice_id"], v["name"]) for v in pool if (v["voice_id"], v["name"]) not in picked]
    return picked, True


def voice_for(persona: str) -> tuple[str | None, str | None]:
    """(voice_id, display name) for a persona: the best candidate."""
    cands, _ = candidates(persona)
    return cands[0] if cands else (None, None)


def cache_dir() -> str:
    d = os.path.join(os.path.dirname(os.path.abspath(db.DB_PATH)), "tts_cache")
    os.makedirs(d, exist_ok=True)
    return d


def _cache_path(vid: str, settings: dict, text: str, lang: str = "", agent: str = "") -> str:
    key = hashlib.sha256(f"{model()}|{vid}|{lang}|{sorted(settings.items())}|{agent}|{text}".encode()).hexdigest()[:40]
    return os.path.join(cache_dir(), key + ".mp3")


def synthesize(text: str, persona: str, agent: str = "") -> Result:
    """Return a path to an mp3 of `text` in the persona's voice, or a reason there isn't one.
    With `agent` (a specialist name) the line is spoken with that agent's voice settings, trimmed to the agent
    length cap instead of refused, cached per agent, and limited to a share of the monthly budget."""
    global _blocked_until, _blocked_reason
    agent = agent.lower() if agent.lower() in AGENT_VOICES else ""
    text = " ".join(text.split())
    if agent:
        persona = AGENT_VOICES[agent][0]
        text = trim_for_speech(text, agent_max_chars())
    if not text:
        return Result(None, "empty")
    if len(text) > MAX_CHARS:
        return Result(None, "too_long")
    if not configured():
        return Result(None, "not_configured")
    if time.monotonic() < _blocked_until:
        return Result(None, "cooldown", detail=_blocked_reason[1])

    def block(reason: str, detail: str) -> Result:
        global _blocked_until, _blocked_reason
        if reason in ("auth", "quota", "permission"):
            _blocked_until, _blocked_reason = time.monotonic() + COOLDOWN_SECONDS, (reason, detail)
        return Result(None, reason, detail=detail)

    try:
        wkey = f"{persona}:{agent}"
        cands = [_working[wkey]] if wkey in _working else candidates(persona, agent)[0]
    except httpx.HTTPStatusError as e:
        return block(*_err(e.response))
    except Exception:
        return Result(None, "error")
    if not cands:
        return Result(None, "no_voice")

    settings = AGENT_VOICES[agent][1] if agent else SETTINGS.get(persona, SETTINGS["hype"])
    lang = LANG.get(persona, "")
    cost = len(text) * credits_per_char()
    month = db.now()[:7]
    last_detail = ""
    for vid, vname in cands:
        path = _cache_path(vid, settings, text, lang, agent)
        if os.path.exists(path):
            _working[wkey] = (vid, vname)
            return Result(path, cached=True, voice=vname)  # free: no credits spent
        if db.tts_usage(month) + cost > budget():
            return Result(None, "budget")
        if agent and db.tts_usage(month) + cost > budget() * agent_budget_share():
            return Result(None, "agent_budget")   # specialists get text only; the coach keeps the rest of the credits
        try:
            with _http() as c:
                r = c.post(f"{BASE}/text-to-speech/{vid}", params={"output_format": "mp3_44100_64"},
                           headers={**_headers(), "Accept": "audio/mpeg"},
                           json={"text": text, "model_id": model(), "voice_settings": {**settings, "use_speaker_boost": False},
                                 **({"language_code": lang} if lang else {})})
        except Exception:
            return Result(None, "error")
        if r.status_code == 200 and r.content:
            tmp = path + ".part"
            with open(tmp, "wb") as f:
                f.write(r.content)
            os.replace(tmp, path)  # never leave a half-written file in the cache
            db.add_tts_usage(month, cost)
            _working[wkey] = (vid, vname)
            return Result(path, credits=cost, voice=vname)
        reason, detail = _err(r)
        if reason in ("voice_not_found", "voice_unavailable"):
            last_detail = detail
            continue                # that voice isn't usable on this account; try the next one
        return block(reason, detail)
    return Result(None, "no_voice", detail=last_detail)


def test(persona: str) -> dict:
    """Say a short sample for real and report exactly what happened. Costs a few credits the first time, then is cached."""
    persona = persona if persona in PREFERRED else "hype"
    res = synthesize(SAMPLES[persona], persona)
    st = status()
    return {"ok": bool(res.path), "reason": res.reason, "detail": res.detail, "voice": res.voice, "cached": res.cached,
            "credits": res.credits, "used": st["used"], "budget": st["budget"], "sample": SAMPLES[persona]}


def status(probe: bool = False) -> dict:
    """Cheap by default. probe=True also checks the key and resolves each persona's voice (one API call)."""
    used = db.tts_usage(db.now()[:7])
    try:
        cached = len([f for f in os.listdir(cache_dir()) if f.endswith(".mp3")])
    except OSError:
        cached = 0
    out = {"configured": configured(), "model": model(), "credits_per_char": credits_per_char(), "used": round(used),
           "budget": round(budget()), "remaining": max(0, round(budget() - used)), "cached_lines": cached, "voices": {}}
    if probe and configured():
        try:
            names, limited = {}, False
            for p in PREFERRED:
                cands, can_list = candidates(p)
                limited = limited or not can_list
                names[p] = cands[0][1] if cands else None
            out["voices"] = names
            out["limited_key"] = limited          # key works but cannot list voices (restricted key)
            out["ok"] = True
        except httpx.HTTPStatusError as e:
            out["ok"] = False
            out["error"], out["detail"] = _err(e.response)
        except Exception:
            out["ok"], out["error"] = False, "error"
    return out
