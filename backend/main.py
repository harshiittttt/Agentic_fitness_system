from __future__ import annotations

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from backend.agents import orchestrator as orch
from backend.memory import db

from contextlib import asynccontextmanager  # noqa: E402


@asynccontextmanager
async def _lifespan(_app):
    """Start the background scheduler (daily readiness, weekly review, nightly memory, experiments). Off with FITNESS_SCHEDULER=0."""
    from fitness_os import runner
    runner.start()
    yield
    runner.stop()


app = FastAPI(title="AI Fitness Agentic System", lifespan=_lifespan)
from backend.auth import TokenAuthMiddleware  # noqa: E402  (optional bearer token: API_TOKEN env; no-op when unset)
app.add_middleware(TokenAuthMiddleware)   # added before CORS so CORS stays outermost and 401s carry CORS headers
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

from fitness_os.api import router as agents_router  # noqa: E402  (Agent OS: events, decision feed, approve/reject/revert)

app.include_router(agents_router)
from backend.routes_session import router as session_router  # noqa: E402  (undo a set, workout summary, weigh-in)
app.include_router(session_router)
from backend.routes_insights import router as insights_router  # noqa: E402  (PRs, recent sessions, today state, meal undo)
app.include_router(insights_router)
from backend.routes_portion import router as portion_router  # noqa: E402  (portion edit / exact-gram log)
app.include_router(portion_router)
from backend.routes_coach_memory import router as coach_memory_router  # noqa: E402  (what the coach learned about you + split position)
app.include_router(coach_memory_router)
from backend.routes_coach import router as coach_router  # noqa: E402  (coach chat with action cards + undo)
app.include_router(coach_router)
from fitness_os.mem.api import router as memory_router  # noqa: E402  (the athlete model: what the system knows about you, confirm / correct / forget)
app.include_router(memory_router)
from backend.wearables import router as wearables_router  # noqa: E402  (provider-neutral wearable ingest, Apple Health / CSV import)
app.include_router(wearables_router)
from fitness_os.push_api import router as push_router  # noqa: E402  (phone push: token, categories, test)
app.include_router(push_router)
from backend.routes_split_consult import router as split_consult_router  # noqa: E402  (the split consult after onboarding: talk it through, then confirm)
app.include_router(split_consult_router)
from backend.routes_diet import router as diet_router  # noqa: E402  (the diet consultant: chat, day/week plans, swaps, preferences)
app.include_router(diet_router)
from backend.routes_voice import router as voice_router  # noqa: E402  (universal voice commands: food, navigation, settings, workout, tools)
app.include_router(voice_router)


def _run_agents_bg(event_type: str, payload: dict) -> None:
    """Agents run after the response so the app never waits on the model. Any failure is swallowed: logging already succeeded."""
    try:
        from fitness_os import loop
        loop.run_event(event_type, payload, record=False)
    except Exception:
        pass


class Profile(BaseModel):
    age: int
    sex: str = "m"
    height_cm: float
    weight_kg: float
    goal: str = Field("maintain", pattern="^(cut|bulk|maintain)$")
    days_per_week: int = 4
    activity: str = "moderate"
    experience: str = Field("intermediate", pattern="^(beginner|intermediate|advanced)$")
    injuries: list[str] = []
    avoid: list[str] = []
    name: str = ""
    coach_persona: str = Field("hype", pattern="^(hype|mentor|straight|haryanvi|roast)$")
    equipment: list[str] = []   # barbell / dumbbell / machine / cable / bodyweight; empty = a full gym


class ProfilePatch(BaseModel):
    """Any subset of profile fields. Omitted fields are left alone."""
    name: str | None = Field(None, max_length=30)
    age: int | None = Field(None, ge=14, le=100)
    sex: str | None = Field(None, pattern="^(m|f)$")
    height_cm: float | None = Field(None, ge=100, le=250)
    weight_kg: float | None = Field(None, ge=30, le=300)
    goal: str | None = Field(None, pattern="^(cut|bulk|maintain)$")
    activity: str | None = Field(None, pattern="^(sedentary|light|moderate|high)$")
    days_per_week: int | None = Field(None, ge=3, le=5)
    experience: str | None = Field(None, pattern="^(beginner|intermediate|advanced)$")
    injuries: list[str] | None = None
    avoid: list[str] | None = None
    coach_persona: str | None = Field(None, pattern="^(hype|mentor|straight|haryanvi|roast)$")
    rebuild: bool = False   # also rebuild the training plan if training inputs changed


class VoiceLog(BaseModel):
    session: str
    transcript: str


class SetLog(BaseModel):
    """One set entered by hand with the steppers: structured, so nothing has to be parsed from speech."""
    session: str
    exercise: str = Field(min_length=1, max_length=60)
    weight: float = Field(0, ge=0, le=600)       # kg added to the bar (0 = bodyweight)
    reps: int = Field(ge=1, le=200)              # seconds for holds such as plank
    rpe: float | None = Field(None, ge=1, le=10)


class Checkin(BaseModel):
    sleep_h: float | None = None
    soreness: int | None = Field(None, ge=1, le=5)
    energy: int | None = Field(None, ge=1, le=5)
    bodyweight: float | None = None
    note: str | None = None


class Chat(BaseModel):
    message: str


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/onboard")
def onboard(p: Profile):
    plan = orch.onboard(p.model_dump())
    from backend.routes_split_consult import mark_pending  # the new split still needs its talk with the coach (first run)
    mark_pending()
    return plan


@app.get("/profile")
def profile_get():
    prof = db.get_profile()
    if not prof:
        raise HTTPException(404, "No profile yet")
    plan = db.get_plan() or {}
    return {"profile": prof, "plan": {"goal": plan.get("goal"), "days": len(plan.get("days", [])), "volume_mult": plan.get("volume_mult"),
                                      "nutrition": plan.get("nutrition")}}


@app.post("/profile")
def profile_update(p: ProfilePatch):
    from backend.tools.profile_edit import update_profile
    body = p.model_dump(exclude={"rebuild"})
    try:
        result = update_profile(body, rebuild=p.rebuild)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {**result, **profile_get()}


@app.get("/plan")
def plan():
    from backend.tools.plan_edit import plan_view
    p = plan_view()
    if not p:
        raise HTTPException(404, "No plan yet")
    return p


class SwapReq(BaseModel):
    day_index: int
    exercise: str
    replacement: str
    scope: str = Field("today", pattern="^(today|plan)$")


class FitReq(BaseModel):
    minutes: int = Field(ge=10, le=180)


class CoachSettings(BaseModel):
    persona: str | None = Field(None, pattern="^(hype|mentor|straight|haryanvi|roast)$")
    name: str | None = None


class LiveSay(BaseModel):
    transcript: str
    ambient: bool = False
    fast: bool = False       # a button press: answer with the instant template line instead of waiting for the composed one


class LiveSet(BaseModel):
    """One set from the workout screen's dials."""
    exercise: str | None = Field(None, max_length=60)    # omitted = the exercise the session is on
    weight: float = Field(0, ge=0, le=600)
    reps: int = Field(ge=1, le=200)
    rpe: float | None = Field(None, ge=1, le=10)
    sets: int = Field(1, ge=1, le=10)


class LiveCue(BaseModel):
    event: str


@app.get("/coach")
def coach_get():
    from backend.coach import persona as P
    prof = db.get_profile() or {}
    cur = P.get(prof.get("coach_persona"))
    return {"persona": prof.get("coach_persona", P.DEFAULT), "user_name": prof.get("name", ""),
            "coach_name": cur["name"], "tts": cur["tts"], "personas": P.listing()}


@app.post("/coach")
def coach_set(c: CoachSettings):
    prof = db.get_profile()
    if not prof:
        raise HTTPException(400, "Finish setup first")
    if c.persona:
        prof["coach_persona"] = c.persona
    if c.name is not None:
        prof["name"] = c.name.strip()[:30]
    db.save_profile(prof)
    return coach_get()


@app.get("/coach/sample")
def coach_sample(persona: str = "hype"):
    from backend.coach import lines, persona as P
    prof = db.get_profile() or {}
    return {"line": lines.pick(persona if persona in P.PERSONAS else P.DEFAULT, "sample", prof.get("name", "")), "tts": P.get(persona)["tts"]}


@app.get("/tts")
def tts(text: str, persona: str = "hype", agent: str = ""):
    """Coach voice audio (ElevenLabs). 503 + {"fallback": true} tells the app to use the phone's own voice."""
    from backend.coach import persona as P, voice
    res = voice.synthesize(text, persona if persona in P.PERSONAS else P.DEFAULT, agent=agent)
    if res.path:
        return FileResponse(res.path, media_type="audio/mpeg", headers={"X-Voice-Cached": "1" if res.cached else "0"})
    return JSONResponse({"fallback": True, "reason": res.reason, "detail": res.detail}, status_code=503)


@app.get("/tts/test")
def tts_test(persona: str = "hype"):
    """Speak a short sample for real and report exactly what ElevenLabs said (used by Profile -> Test the voice)."""
    from backend.coach import voice
    return voice.test(persona)


@app.get("/tts/status")
def tts_status(probe: bool = False):
    from backend.coach import voice
    return voice.status(probe)


@app.get("/live/state")
def live_state():
    from backend.coach import live
    return live.session_state()


@app.post("/live/start")
def live_start():
    from backend.coach import live
    return live.start()


@app.post("/live/say")
def live_say(r: LiveSay):
    from backend.coach import live
    return live.handle(r.transcript, ambient=r.ambient, fast=r.fast)


@app.post("/live/log")
def live_log(s: LiveSet):
    from backend.coach import live
    return live.log_set(s.exercise, s.weight, s.reps, s.rpe, s.sets)


@app.post("/live/cue")
def live_cue(r: LiveCue):
    from backend.coach import live
    return live.cue(r.event)


@app.post("/live/end")
def live_end():
    from backend.coach import live
    return live.end()


@app.get("/exercises/alternatives")
def exercise_alternatives(name: str):
    from backend.tools.plan_edit import get_alternatives
    return get_alternatives(name)


@app.post("/plan/swap")
def plan_swap(r: SwapReq):
    from backend.tools.plan_edit import swap
    try:
        return swap(r.day_index, r.exercise.lower(), r.replacement.lower(), r.scope)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/session/fit")
def session_fit(r: FitReq):
    from backend.tools.plan_edit import fit_today
    try:
        return fit_today(r.minutes)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/session/restore")
def session_restore():
    from backend.tools.plan_edit import clear_override
    return clear_override()


class MealLog(BaseModel):
    transcript: str
    day: str | None = None   # YYYY-MM-DD; defaults to today


class MealRepeat(BaseModel):
    id: int
    day: str | None = None


def _valid_day(day: str | None) -> str | None:
    from datetime import date
    if day is None:
        return None
    try:
        return date.fromisoformat(day).isoformat()
    except ValueError:
        raise HTTPException(400, "day must be YYYY-MM-DD")


@app.post("/meal")
def meal(m: MealLog):
    from backend.tools.meal_log import log_meal
    return log_meal(m.transcript, _valid_day(m.day))


@app.post("/meal/repeat")
def meal_repeat(r: MealRepeat):
    from backend.tools.meal_log import repeat_meal
    if not repeat_meal(r.id, _valid_day(r.day)):
        raise HTTPException(404, "No such meal")
    return {"ok": True}


@app.get("/meal/recent")
def meal_recent():
    return db.recent_meals(10)


@app.get("/nutrition/week")
def nutrition_week():
    from backend.tools.meal_log import week_summary
    return week_summary()


@app.get("/nutrition/day")
def nutrition_day(day: str):
    from backend.tools.meal_log import today_summary
    return today_summary(_valid_day(day))


@app.get("/nutrition/today")
def nutrition_today():
    from backend.tools.meal_log import today_summary
    return today_summary()


@app.delete("/meal/{meal_id}")
def meal_delete(meal_id: int):
    db.delete_meal(meal_id)
    return {"ok": True}


@app.get("/progress/series")
def progress_series():
    from backend.agents.specialists import series_report
    return series_report()


@app.post("/demo/seed")
def demo_seed():
    from backend.tools.demo import seed
    return seed()


@app.post("/reset-all")
def reset_all():
    db.reset_all()
    return {"ok": True}


@app.post("/demo/reset")
def demo_reset():
    db.reset_history()
    return {"ok": True}


@app.post("/log")
def log(v: VoiceLog):
    return orch.log_and_coach(v.session, v.transcript)


@app.post("/log/set")
def log_set(v: SetLog):
    """Same result shape as /log (saved, next_set, safety_flag), so the app handles both identically."""
    name = v.exercise.strip().lower()
    parsed = {"sets": [{"exercise": name, "weight": v.weight, "reps": v.reps, **({"rpe": v.rpe} if v.rpe is not None else {})}],
              "safety_note": ""}
    return orch.log_and_coach(v.session, f"{name} {v.weight:g} for {v.reps}", parsed=parsed)


@app.get("/exercise/prefill")
def exercise_prefill(name: str, target_reps: int = 8, target_rpe: float = 8.0):
    """What the set logger starts from: your last set of this lift and the autoregulated next load.
    No history -> both are null and the app asks for a starting weight (nothing is invented)."""
    from backend.tools import calculators as calc
    rows = [r for r in db.get_sets(name, 20) if r.get("reps")]
    if not rows:
        return {"last": None, "suggest": None, "increment": 2.5}
    last = rows[0]
    suggest = None
    if last.get("weight") is not None and last.get("reps"):
        suggest = calc.next_set_suggestion(float(last["weight"]), int(last["reps"]), last.get("rpe"), target_reps, target_rpe)
    return {"last": {"weight": last["weight"], "reps": last["reps"], "rpe": last.get("rpe"), "session": last["session"], "ts": last["ts"]},
            "suggest": suggest, "increment": 2.5}


@app.get("/readiness")
def readiness():
    from backend.agents.specialists import recovery_assess
    return recovery_assess()


@app.post("/checkin")
def checkin(c: Checkin, bg: BackgroundTasks):
    db.add_checkin(c.sleep_h, c.soreness, c.energy, c.bodyweight, c.note)
    bg.add_task(_run_agents_bg, "checkin.logged", c.model_dump())  # event -> agents propose -> critic -> gate -> ledger
    return {"ok": True}


@app.post("/chat")
def chat(c: Chat, bg: BackgroundTasks):
    from backend.policy.adaptation import has_safety_flag
    if has_safety_flag(c.message):  # pain mentioned in chat is an event, not just a reply
        bg.add_task(_run_agents_bg, "pain.report", {"text": c.message})
    return orch.chat_with_actions(c.message)   # {"reply", "actions"}: old clients just read reply


@app.post("/review")
def review():
    return orch.review()


@app.get("/changes")
def changes():
    return db.get_changes()


@app.get("/progress")
def progress():
    from backend.agents.specialists import analyst_report
    return analyst_report()
