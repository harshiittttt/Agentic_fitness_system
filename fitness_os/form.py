"""Form (vision) agent: technique cues from pose keypoints (deterministic) or, optionally, from a few frames (Gemini vision).

Docs: "Form (vision): video technique analysis, cue generation. Uploaded clips, exercise library. Cues, load caps."
Here the agent is a coach's second pair of eyes, never a clinician:
  * Output is hedged observations ("seems", "might", "worth a look"), never a diagnosis, never a load change.
  * Proposals are `note` / `nudge` only (SCOPES["form"]); no volume, calories or exercises. Load caps stay with Training / Recovery.
  * Pain language (in the athlete's note) is NOT coached through: the agent stops technique talk and routes to the recovery path
    (event `pain.report`). Red-flag words use the same list as the watchdogs.
  * Works with no key and no video. Input kinds: pose keypoints (JSON) -> deterministic analysis; frames + key -> vision path whose
    output is only a choice of known cue ids / statuses (the wording always comes from the templates below, so it is grounded);
    anything else -> an explicit "no video analysis available" result. A failing model call falls back to that same result.

Keypoint format (normalised image coords, y grows downward, like MediaPipe / COCO / MoveNet):
    {"exercise": "squat|deadlift|bench|press", "fps": 30, "view": "side|front|auto",
     "frames": [{"t": 0.0, "kp": {"left_hip": [x, y, score?], "left_knee": ..., "left_ankle": ..., "left_shoulder": ..., "left_wrist": ...,
                                  "right_*": ..., "bar": [x, y]?, "mid_back": [x, y]?}}, ...]}
"""
from __future__ import annotations

import base64
import math
import statistics

from backend.agents.base import LLMError, have_key

EXERCISES = ("squat", "deadlift", "bench", "press")
CUES = ("depth", "knee_valgus", "back_position", "bar_path", "tempo")
MAX_FRAMES = 3000
MIN_FRAMES = 12
MAX_KEYPOINT_CHARS = 3_000_000
MAX_IMAGES = 6
MAX_IMAGE_B64_CHARS = 1_500_000
MIN_SCORE = 0.3
DISCLAIMER = "Coaching observations from a few points on a video, not a medical or injury assessment. Stop if something hurts and tell your coach."

NO_ANALYSIS = ("No video analysis available: there is no pose-keypoint data and no vision model to look at a video. "
               "Send keypoints (JSON) from a pose tool, or film side-on and add a Gemini key, and I can comment on technique.")

PAIN_WORDS = ("pain", "hurt", "sharp", "pinch", "pop", "twinge", "ache", "burning", "stab", "strain", "injur", "tweak", "swelling", "sore joint",
              "दर्द")


def pain_language(text: str) -> str | None:
    from .watchdogs import red_flag
    t = (text or "").lower()
    return red_flag(t) or next((w for w in PAIN_WORDS if w in t), None)


# ------------------------------------------------------------------ validation ----------------------------------------------------------
class FormInputError(ValueError):
    pass


def _num(v) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or abs(v) > 1e5:
        raise FormInputError("keypoint values must be finite numbers")
    return float(v)


def validate_keypoints(data) -> dict:
    """-> {exercise, fps, view, frames: [{t, kp: {name: (x, y)}}]} or raises FormInputError. Low-score points are dropped here."""
    if not isinstance(data, dict):
        raise FormInputError("keypoints must be an object with a 'frames' list")
    frames = data.get("frames")
    if not isinstance(frames, list) or not frames:
        raise FormInputError("'frames' must be a non-empty list")
    if len(frames) > MAX_FRAMES:
        raise FormInputError(f"too many frames ({len(frames)}); the limit is {MAX_FRAMES}")
    ex = str(data.get("exercise") or "squat").lower()
    if ex not in EXERCISES:
        raise FormInputError(f"exercise must be one of {', '.join(EXERCISES)}")
    fps = _num(data.get("fps", 30))
    if not 1 <= fps <= 240:
        raise FormInputError("fps must be between 1 and 240")
    view = str(data.get("view") or "auto").lower()
    if view not in ("side", "front", "auto"):
        raise FormInputError("view must be side, front or auto")
    out = []
    for i, f in enumerate(frames):
        kp = f.get("kp") if isinstance(f, dict) else None
        if not isinstance(kp, dict) or len(kp) > 60:
            raise FormInputError(f"frame {i}: 'kp' must be an object of named points")
        pts = {}
        for name, v in kp.items():
            if not isinstance(name, str) or len(name) > 40 or not isinstance(v, (list, tuple)) or len(v) not in (2, 3):
                raise FormInputError(f"frame {i}: point '{str(name)[:40]}' must be [x, y] or [x, y, score]")
            x, y = _num(v[0]), _num(v[1])
            if len(v) == 3 and _num(v[2]) < MIN_SCORE:
                continue
            pts[name] = (x, y)
        t = _num(f["t"]) if "t" in f else i / fps
        out.append({"t": t, "kp": pts})
    if any(b["t"] < a["t"] for a, b in zip(out, out[1:])):
        raise FormInputError("frame times must not go backwards")
    return {"exercise": ex, "fps": fps, "view": view, "frames": out}


# ------------------------------------------------------------------ geometry ------------------------------------------------------------
def _pt(frame: dict, base: str):
    """Average of the left/right points that exist (or the named point itself)."""
    kp = frame["kp"]
    if base in kp:
        return kp[base]
    got = [kp[s + base] for s in ("left_", "right_") if s + base in kp]
    return (sum(p[0] for p in got) / len(got), sum(p[1] for p in got) / len(got)) if got else None


def _d(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _med(xs):
    return statistics.median(xs) if xs else None


def _smooth(xs: list[float], k: int = 2) -> list[float]:
    return [sum(xs[max(0, i - k):i + k + 1]) / len(xs[max(0, i - k):i + k + 1]) for i in range(len(xs))]


def _signal(frames: list[dict], ex: str):
    """The vertical position that rises and falls with each rep (hips for squat / deadlift, bar or wrists for bench / press)."""
    names = ("hip",) if ex in ("squat", "deadlift") else ("bar", "wrist")
    for n in names:
        ys = [(_pt(f, n) or (None, None))[1] for f in frames]
        if sum(y is not None for y in ys) >= 0.8 * len(ys):
            last = next(y for y in ys if y is not None)
            filled = []
            for y in ys:
                last = y if y is not None else last
                filled.append(last)
            return _smooth(filled)
    return None


def find_reps(frames: list[dict], ex: str) -> list[dict]:
    """Hysteresis rep finder on the vertical signal; bottom = largest y. -> [{start, bottom, end}] frame indexes."""
    y = _signal(frames, ex)
    if not y:
        return []
    lo_v, hi_v = min(y), max(y)
    rng = hi_v - lo_v
    if rng < 0.04:     # barely moves: no reps to speak of
        return []
    t_hi, t_lo = lo_v + 0.65 * rng, lo_v + 0.35 * rng
    reps, state, start, best = [], "top", 0, None
    for i, v in enumerate(y):
        if state == "top":
            if v <= t_lo:
                start = i
            elif v >= t_hi:
                state, best = "bottom", i
        elif v >= y[best]:
            best = i
        elif v <= t_lo:
            reps.append({"start": start, "bottom": best, "end": i})
            state, start = "top", i
    return reps


def _cue(id_, status, text, **evidence) -> dict:
    return {"id": id_, "status": status, "text": text, "evidence": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in evidence.items()}}


def _insufficient(id_, why) -> dict:
    return _cue(id_, "insufficient", f"I can't judge this from the points I have ({why}).")


# ------------------------------------------------------------------ cues ----------------------------------------------------------------
def _depth(frames, reps, ex):
    if ex != "squat":
        return _cue("depth", "n/a", "Depth is only checked for squats.")
    vals = []
    for r in reps:
        f = frames[r["bottom"]]
        hip, knee = _pt(f, "hip"), _pt(f, "knee")
        if hip and knee:
            thigh = _d(hip, knee)
            if thigh > 1e-6:
                vals.append((hip[1] - knee[1]) / thigh)
    if not vals:
        return _insufficient("depth", "hip and knee points are missing at the bottom")
    m = _med(vals)
    if m >= -0.05:
        return _cue("depth", "ok", "Depth looks like at least about parallel on these points.", hip_below_knee_ratio=m, reps_measured=len(vals))
    return _cue("depth", "watch", "Your hips might be stopping a little above knee height at the bottom. If you want more depth, a slightly lighter weight "
                "and a pause at the bottom could help; camera angle can also fool this, so a side-on clip would confirm.",
                hip_below_knee_ratio=m, reps_measured=len(vals))


def _valgus(frames, reps, ex):
    if ex != "squat":
        return _cue("knee_valgus", "n/a", "Knee tracking is only checked for squats.")
    ch = []
    for r in reps:
        def ratio(i):
            kp = frames[i]["kp"]
            need = ("left_knee", "right_knee", "left_ankle", "right_ankle")
            if not all(n in kp for n in need):
                return None
            aw = abs(kp["left_ankle"][0] - kp["right_ankle"][0])
            return abs(kp["left_knee"][0] - kp["right_knee"][0]) / aw if aw > 1e-3 else None
        a, b = ratio(r["start"]), ratio(r["bottom"])
        if a and b:
            ch.append(b / a)
    if not ch:
        return _insufficient("knee_valgus", "it needs both knees and ankles, ideally filmed from the front")
    m = _med(ch)
    if m < 0.85:
        return _cue("knee_valgus", "watch", "Your knees seem to drift inward as you come out of the bottom. A cue like 'spread the floor' or "
                    "pushing the knees out over the toes might help; it is worth a look from the front.", knee_gap_vs_standing=m, reps_measured=len(ch))
    return _cue("knee_valgus", "ok", "Knee tracking looks steady relative to your feet.", knee_gap_vs_standing=m, reps_measured=len(ch))


def _torso_angle(f):
    s, h = _pt(f, "shoulder"), _pt(f, "hip")
    if not s or not h or _d(s, h) < 1e-6:
        return None
    return abs(math.degrees(math.atan2(s[0] - h[0], h[1] - s[1])))


def _back(frames, reps, ex):
    if ex not in ("squat", "deadlift"):
        return _cue("back_position", "n/a", "Back position is only checked for squats and deadlifts.")
    curv, lean = [], []
    for r in reps:
        f = frames[r["bottom"]]
        s, h, m = _pt(f, "shoulder"), _pt(f, "hip"), f["kp"].get("mid_back") or f["kp"].get("upper_back")
        if s and h and m and _d(s, h) > 1e-6:
            L = _d(s, h)
            curv.append(abs((h[0] - s[0]) * (s[1] - m[1]) - (s[0] - m[0]) * (h[1] - s[1])) / L / L)   # distance of the mid-back point from the shoulder-hip line
        a = _torso_angle(f)
        if a is not None:
            lean.append(a)
    if curv:
        c = _med(curv)
        if c > 0.12:
            return _cue("back_position", "watch", "Your upper back might be rounding a bit at the bottom. Keeping your chest up and bracing before each "
                        "rep may help; a lighter set filmed side-on would show whether it is consistent.", spine_bow=c, reps_measured=len(curv))
        return _cue("back_position", "ok", "Your back looks fairly neutral at the bottom on these points.", spine_bow=c, reps_measured=len(curv))
    if lean and ex == "squat" and _med(lean) > 65:
        return _cue("back_position", "watch", "You seem to lean forward quite a lot at the bottom. That can be build or mobility, not necessarily a fault; "
                    "a side-on clip with a mid-back point would tell me more.", torso_lean_deg=_med(lean))
    return _insufficient("back_position", "rounding needs an extra mid-back point; I only have shoulder and hip")


def _bar_path(frames, reps, ex):
    if not reps:
        return _insufficient("bar_path", "no clear reps found")
    dev = []
    for r in reps:
        seg = frames[r["bottom"]:r["end"] + 1]
        pts = [p for p in ((f["kp"].get("bar") or _pt(f, "wrist")) for f in seg) if p]
        if len(pts) < 3 or (ex == "squat" and not any("bar" in f["kp"] for f in seg)):
            continue
        ref = _d(_pt(frames[r["bottom"]], "shoulder"), _pt(frames[r["bottom"]], "hip")) if _pt(frames[r["bottom"]], "shoulder") and _pt(frames[r["bottom"]], "hip") else None
        vert = max(p[1] for p in pts) - min(p[1] for p in pts)
        scale = ref if ref and ref > 1e-3 else vert
        if scale > 1e-3:
            dev.append((max(p[0] for p in pts) - min(p[0] for p in pts)) / scale)
    if not dev:
        return _insufficient("bar_path", "it needs a bar point (or wrists) tracked through the lift")
    m = _med(dev)
    if m > 0.3:
        return _cue("bar_path", "watch", "The bar path seems to wander sideways on the way up. Staying over the mid-foot (or, for a bench, a steady "
                    "line to the shoulders) usually feels more efficient; worth checking with a side-on clip.", sideways_travel=m, reps_measured=len(dev))
    return _cue("bar_path", "ok", "The bar path looks fairly straight.", sideways_travel=m, reps_measured=len(dev))


def _tempo(frames, reps, fps):
    if not reps:
        return _insufficient("tempo", "no clear reps found")
    down = [frames[r["bottom"]]["t"] - frames[r["start"]]["t"] for r in reps]
    up = [frames[r["end"]]["t"] - frames[r["bottom"]]["t"] for r in reps]
    md, mu = _med(down), _med(up)
    ev = dict(reps=len(reps), lowering_s=md, lifting_s=mu)
    if len(up) >= 3 and up[-1] > 1.4 * up[0] and up[-1] - up[0] > 0.4:
        return _cue("tempo", "ok", f"Your last rep was noticeably slower than your first ({up[0]:.1f}s to {up[-1]:.1f}s), which is normal near the end of a hard set.", **ev)
    if md is not None and md < 0.7:
        return _cue("tempo", "watch", "Your lowering phase seems quick (under a second). A slower, controlled descent of 2 to 3 seconds often helps "
                    "position and feel; you can try it on lighter sets.", **ev)
    return _cue("tempo", "ok", f"Your tempo looks steady (about {md:.1f}s down, {mu:.1f}s up).", **ev)


# ------------------------------------------------------------------ analyzers ------------------------------------------------------------
def analyze_keypoints(data) -> dict:
    d = validate_keypoints(data)
    frames, ex = d["frames"], d["exercise"]
    if len(frames) < MIN_FRAMES:
        return {"available": False, "mode": "keypoints", "exercise": ex, "cues": [], "reps": 0,
                "message": f"Too few frames to judge ({len(frames)}; need at least {MIN_FRAMES}). Film a full set or at least one whole rep."}
    reps = find_reps(frames, ex)
    cues = [_depth(frames, reps, ex), _valgus(frames, reps, ex), _back(frames, reps, ex), _bar_path(frames, reps, ex), _tempo(frames, reps, d["fps"])]
    if not reps:
        msg = "No clear reps were found in the movement, so most cues could not be judged."
    else:
        msg = f"Looked at {len(reps)} rep{'s' if len(reps) != 1 else ''} of {ex} from pose points."
    return {"available": True, "mode": "keypoints", "exercise": ex, "reps": len(reps), "cues": cues, "message": msg}


VISION_PROMPT = ("You look at a few frames of a barbell lift and report ONLY which technique cues you can actually see. For each cue id in "
                 f"{list(CUES)} answer status 'ok', 'watch' or 'cannot_tell'. Use 'cannot_tell' whenever the angle, lighting or crop does not "
                 "show it. Do not diagnose, do not mention pain or injury, do not suggest weights. If the video is not a lift, answer cannot_tell for all.")


def _vision_call(ex: str, images_b64: list[str]) -> dict:
    from backend.agents import base
    parts = [{"type": "text", "text": f"Exercise: {ex}. Frames in time order."}]
    parts += [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b}"}} for b in images_b64]
    tool = {"name": "report_cues", "description": "Report technique cues seen.",
            "input_schema": {"type": "object", "properties": {"cues": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "string", "enum": list(CUES)}, "status": {"type": "string", "enum": ["ok", "watch", "cannot_tell"]}},
                "required": ["id", "status"]}}}, "required": ["cues"]}}
    body = {"messages": [{"role": "system", "content": VISION_PROMPT}, {"role": "user", "content": parts}], "max_tokens": 400,
            "tools": base._openai_tools([tool]), "tool_choice": {"type": "function", "function": {"name": "report_cues"}}}
    msg = base._post("main", body, timeout=60.0, retries=0)["choices"][0]["message"]
    for tc in msg.get("tool_calls") or []:
        try:
            import json
            return json.loads(tc["function"].get("arguments") or "{}")
        except ValueError:
            continue
    return {}


_VISION_TEXT = {  # grounded wording: the model only picks the cue id and status
    "depth": "From the frames, depth might be a little shallow. Worth confirming with a clean side-on clip.",
    "knee_valgus": "In the frames your knees might be caving in slightly. Worth a look from the front; 'push the knees out' is a common cue.",
    "back_position": "Your back might be rounding a bit in some frames. Bracing and keeping the chest up could help; confirm side-on on a lighter set.",
    "bar_path": "The bar might drift away from a straight line in the frames. Worth checking with a side-on clip.",
    "tempo": "Your lowering might be quick in the frames. A slower, controlled descent on lighter sets could help.",
}


def analyze_frames(exercise: str, images_b64: list[str], *, caller=_vision_call) -> dict:
    ex = (exercise or "squat").lower()
    if ex not in EXERCISES:
        raise FormInputError(f"exercise must be one of {', '.join(EXERCISES)}")
    if not images_b64 or len(images_b64) > MAX_IMAGES:
        raise FormInputError(f"send between 1 and {MAX_IMAGES} frames")
    for b in images_b64:
        if not isinstance(b, str) or len(b) > MAX_IMAGE_B64_CHARS:
            raise FormInputError(f"each frame must be a base64 string of at most {MAX_IMAGE_B64_CHARS} characters")
        try:
            base64.b64decode(b, validate=True)
        except Exception:
            raise FormInputError("frames must be valid base64 image data")
    if not have_key():
        return no_analysis("no vision model is configured")
    try:
        raw = caller(ex, images_b64)
    except (LLMError, KeyError, IndexError, TypeError, ValueError) as e:
        return no_analysis(f"the vision model failed ({type(e).__name__})")
    seen, cues = set(), []
    for c in (raw.get("cues") if isinstance(raw, dict) else None) or []:
        if not isinstance(c, dict) or c.get("id") not in CUES or c.get("id") in seen or c.get("status") not in ("ok", "watch", "cannot_tell"):
            continue   # anything not on the closed list is dropped (grounding)
        seen.add(c["id"])
        st = c["status"]
        cues.append(_cue(c["id"], "watch" if st == "watch" else "ok" if st == "ok" else "insufficient",
                         _VISION_TEXT[c["id"]] if st == "watch" else "Looks fine in the frames I was given." if st == "ok"
                         else "I can't tell this from the frames (angle or crop).", source="vision"))
    if not cues:
        return no_analysis("the vision model gave no usable answer")
    return {"available": True, "mode": "vision", "exercise": ex, "reps": None, "cues": cues, "message": f"Looked at {len(images_b64)} frame(s) of {ex} with a vision model; treat as a rough second opinion."}


def no_analysis(why: str = "") -> dict:
    return {"available": False, "mode": "none", "exercise": None, "reps": 0, "cues": [], "message": NO_ANALYSIS + (f" ({why})" if why else "")}


# ------------------------------------------------------------------ proposals / entry points --------------------------------------------
def _proposals(res: dict, note: str):
    from .schema import Proposal
    if res.get("escalation"):
        return [Proposal(agent="form", kind="note", params={"text": "Pain mentioned while reviewing a lift video; technique coaching paused and sent to the recovery check."},
                         rationale="You mentioned pain, so I am not coaching technique on this; the recovery check should look first.",
                         evidence=f"pain language: {res['escalation']['matched']}", confidence=0.8)]
    watch = [c for c in res["cues"] if c["status"] == "watch"]
    if not res["available"] or not watch:
        return []
    ids = ", ".join(c["id"].replace("_", " ") for c in watch)
    props = [Proposal(agent="form", kind="note", params={"text": f"Form review ({res['exercise']}, {res['mode']}): worth watching {ids}. Observations only, not a diagnosis."[:240]},
                      rationale=f"The video suggests a few technique points worth watching: {ids}.", evidence=f"{res['mode']}; reps {res.get('reps')}", confidence=0.55)]
    props.append(Proposal(agent="form", kind="nudge", params={"text": watch[0]["text"][:240]},
                          rationale="One hedged technique cue from your lift video.", evidence=f"cue {watch[0]['id']}; {res['mode']}", confidence=0.5))
    return props


def analyze(keypoints=None, frames_b64=None, exercise: str | None = None, note: str = "", *, vision_caller=_vision_call) -> dict:
    """Entry point for API / CLI / loop. Never raises on missing input (returns the explicit fallback); raises FormInputError on bad input."""
    if keypoints is not None:
        res = analyze_keypoints({**keypoints, "exercise": keypoints.get("exercise") or exercise or "squat"} if isinstance(keypoints, dict) else keypoints)
    elif frames_b64:
        res = analyze_frames(exercise or "squat", frames_b64, caller=vision_caller)
    else:
        res = no_analysis()
    hit = pain_language(note)
    res["escalation"] = {"event": "pain.report", "matched": hit, "route": "recovery",
                         "message": "You mentioned pain. I will not coach technique through it; please log it as a pain report so the recovery check can look."} if hit else None
    res["disclaimer"] = DISCLAIMER
    res["proposals"] = [p.model_dump() for p in _proposals(res, note)]
    return res


def status() -> dict:
    return {"agent": "form", "llm": have_key(), "exercises": list(EXERCISES), "cues": list(CUES), "keypoint_analysis": True,
            "vision": have_key(), "limits": {"max_frames": MAX_FRAMES, "max_images": MAX_IMAGES}, "scope": ["note", "nudge"],
            "offline_fallback": "deterministic keypoint analysis, else an explicit 'no video analysis available'"}


def propose(event: str, payload: dict | None = None) -> dict:
    """Loop adapter (loop.OFFLINE): event 'form.review' with payload {keypoints?, frames_b64?, exercise?, note?}."""
    from .schema import Proposal
    payload = payload or {}
    try:
        r = analyze(payload.get("keypoints"), payload.get("frames_b64"), payload.get("exercise"), str(payload.get("note") or ""))
    except FormInputError as e:
        return {"agent": "form", "reasoning": f"I could not read that video data: {e}", "proposals": [], "dropped": []}
    reasoning = r["message"] + (" " + r["escalation"]["message"] if r["escalation"] else "")
    return {"agent": "form", "reasoning": reasoning, "proposals": [Proposal(**p) for p in r["proposals"]], "dropped": []}
