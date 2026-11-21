"""Universal voice layer (POST /voice/command). Offline: conftest strips LLM keys; router tests use a fake run_agent."""
import time
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.coach import live, voice_control as vc
from backend.main import app
from backend.memory import db
from backend.tools.demo import PROFILE

KEYS = {"reply", "intent", "actions", "expects", "moment", "pr", "safety_flag", "state", "saved"}
TODAY = date.today().isoformat()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "vc.db"))
    live._pending_clear()
    vc.reset()
    monkeypatch.setattr(vc, "_fire", lambda *a, **k: None)
    c = TestClient(app)
    c.post("/onboard", json={**PROFILE, "name": "Alex", "coach_persona": "hype"})
    return c


def say(c, text, screen="today", day=None, ambient=False, followup=False):
    r = c.post("/voice/command", json={"transcript": text, "screen": screen, "day": day, "ambient": ambient, "followup": followup})
    assert r.status_code == 200, r.text
    j = r.json()
    check_contract(j)
    return j


def check_contract(j):
    assert KEYS <= set(j), set(j)
    assert j["expects"] in (None, "set_details", "food_amount", "confirm", "answer")
    assert isinstance(j["reply"], str) and isinstance(j["actions"], list)
    for a in j["actions"]:
        assert py_valid(a), a


def py_valid(a):
    """Python mirror of parseAction() in mobile/src/voiceBus.ts (independent of voice_control.valid_action)."""
    import re
    if not isinstance(a, dict) or not isinstance(a.get("type"), str):
        return False
    t = a["type"]
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    ok = {
        "navigate": lambda: a.get("tab") in ("today", "coach", "nutrition", "insights", "setup"),
        "open": lambda: a.get("screen") in ("live", "agenda", "intake", "research", "trace", "knows", "decisions", "roundtable", "scorecard", "review", "server", "push", "diet"),
        "close": lambda: True, "end": lambda: True, "rest_skip": lambda: True,
        "refresh": lambda: all(x in ("today", "nutrition", "plan", "decisions", "profile", "all") for x in a.get("what", [])),
        "nutrition_day": lambda: isinstance(a.get("day"), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", a["day"]) is not None,
        "insights_segment": lambda: isinstance(a.get("seg"), str) and bool(a["seg"]),
        "rest": lambda: num(a.get("seconds")) and 0 < a["seconds"] <= 900,
        "rest_add": lambda: num(a.get("seconds")) and 0 < a["seconds"] <= 300,
        "setting": lambda: a.get("key") in ("wake", "vibration", "voice_mode", "persona") and isinstance(a.get("value"), (str, bool)),
    }
    return t in ok and ok[t]()


def meals(day=TODAY):
    return db.get_meals(day)


# ---------------------------------------------------------------- food
def test_food_all_quantities_logs_immediately(client):
    j = say(client, "hey coach I ate two eggs")
    assert j["intent"] == "meal" and "egg" in j["reply"] and "left" in j["reply"]
    assert [m["name"] for m in meals()] == ["egg"] and round(meals()[0]["grams"]) == 100
    assert {"type": "refresh", "what": ["nutrition"]} in j["actions"]
    assert not any(a["type"] == "navigate" for a in j["actions"])


def test_food_missing_quantity_asks_one_question_at_a_time(client):
    j = say(client, "I had eggs and rice")
    assert j["expects"] == "food_amount" and j["reply"] == "How many eggs?" and meals() == []
    j = say(client, "four", followup=True)
    assert j["expects"] == "food_amount" and "grams of white rice" in j["reply"] and meals() == []
    j = say(client, "three fifty", followup=True)
    assert j["intent"] == "meal" and j["expects"] is None
    got = {m["name"]: round(m["grams"]) for m in meals()}
    assert got == {"egg": 200, "white rice": 350}


def test_food_answer_with_unit_and_usual(client):
    say(client, "I ate rice")
    j = say(client, "350 grams", followup=True)
    assert j["intent"] == "meal" and round(meals()[0]["grams"]) == 350
    say(client, "log eggs")
    say(client, "same as usual", followup=True)
    assert [m["name"] for m in meals()][-1] == "egg" and round(meals()[-1]["grams"]) == 50


def test_food_pending_cancel_and_unrelated_drops_question(client):
    say(client, "I had eggs")
    assert say(client, "never mind", followup=True)["intent"] == "cancelled" and meals() == []
    say(client, "I had eggs")
    j = say(client, "open my calendar")
    assert j["intent"] == "navigate" and meals() == [] and not vc._pend


def test_food_yesterday_and_nutrition_day_action(client):
    y = (date.today() - timedelta(days=1)).isoformat()
    j = say(client, "I ate two rotis yesterday")
    assert j["intent"] == "meal" and meals(y) and not meals()
    assert {"type": "nutrition_day", "day": y} in j["actions"] and "yesterday" in j["reply"]


def test_food_on_fuel_uses_selected_day(client):
    y = (date.today() - timedelta(days=3)).isoformat()
    j = say(client, "add 2 bananas to my snack", screen="nutrition", day=y)
    assert meals(y) and not meals() and not any(a["type"] == "navigate" for a in j["actions"])


def test_food_unknown(client):
    j = say(client, "I ate blorfzonk")
    assert j["intent"] == "meal_unclear" and meals() == [] and "Try: I ate two eggs" in j["reply"]


def test_calories_left_eaten_undo_repeat(client):
    say(client, "I ate 3 eggs")
    j = say(client, "how many calories do I have left")
    assert j["intent"] == "meal_left" and "calories" in j["reply"] and "protein" in j["reply"]
    assert say(client, "what did I eat today")["intent"] == "meal_list" and "egg" in say(client, "what did I eat today")["reply"]
    j = say(client, "repeat my last meal")
    assert j["intent"] == "meal_repeat" and len(meals()) == 2
    j = say(client, "undo that")
    assert j["intent"] == "meal_undo" and "Removed" in j["reply"] and len(meals()) == 1
    j = say(client, "remove the last meal")
    assert len(meals()) == 0 and say(client, "remove the last meal")["reply"] == "There is no meal to remove."


def test_undo_removes_all_items_of_one_utterance(client):
    say(client, "I ate 2 eggs and 3 rotis")
    assert len(meals()) == 2
    say(client, "undo the last meal")
    assert meals() == []


def test_ambient_chatter_is_not_logged_but_command_is(client):
    assert say(client, "I ate two eggs", ambient=True)["intent"] == "ignored" and meals() == []
    assert say(client, "open my calendar", ambient=True)["intent"] == "ignored"
    assert say(client, "hey coach I ate two eggs", ambient=True)["intent"] == "meal" and len(meals()) == 1
    assert say(client, "log 2 bananas", ambient=True)["intent"] == "meal"


def test_gym_talk_with_had_is_not_food(client):
    j = say(client, "I had 3 sets of bench left")
    assert j["intent"] != "meal" and meals() == []


# ---------------------------------------------------------------- navigation
NAV = [
    ("open fuel", [{"type": "navigate", "tab": "nutrition"}]), ("show me my meals", [{"type": "navigate", "tab": "nutrition"}]),
    ("go to nutrition", [{"type": "navigate", "tab": "nutrition"}]), ("take me to food", [{"type": "navigate", "tab": "nutrition"}]),
    ("open my progress", [{"type": "navigate", "tab": "insights"}, {"type": "insights_segment", "seg": "trends"}]),
    ("show me the charts", [{"type": "navigate", "tab": "insights"}, {"type": "insights_segment", "seg": "trends"}]),
    ("open stats", [{"type": "navigate", "tab": "insights"}, {"type": "insights_segment", "seg": "trends"}]),
    ("open my profile", [{"type": "navigate", "tab": "setup"}]), ("go to settings", [{"type": "navigate", "tab": "setup"}]),
    ("open setup", [{"type": "navigate", "tab": "setup"}]),
    ("open my calendar", [{"type": "navigate", "tab": "setup"}, {"type": "open", "screen": "agenda"}]),
    ("show me the agenda", [{"type": "navigate", "tab": "setup"}, {"type": "open", "screen": "agenda"}]),
    ("open trips", [{"type": "navigate", "tab": "setup"}, {"type": "open", "screen": "agenda"}]),
    ("update my profile", [{"type": "navigate", "tab": "setup"}, {"type": "open", "screen": "intake"}]),
    ("open the interview", [{"type": "navigate", "tab": "setup"}, {"type": "open", "screen": "intake"}]),
    ("open evidence", [{"type": "navigate", "tab": "setup"}, {"type": "open", "screen": "research"}]),
    ("show me the research", [{"type": "navigate", "tab": "setup"}, {"type": "open", "screen": "research"}]),
    ("open the trace", [{"type": "navigate", "tab": "setup"}, {"type": "open", "screen": "trace"}]),
    ("show me what the coaches did", [{"type": "navigate", "tab": "setup"}, {"type": "open", "screen": "trace"}]),
    ("what do you know about me", [{"type": "navigate", "tab": "setup"}, {"type": "open", "screen": "knows"}]),
    ("open memory", [{"type": "navigate", "tab": "setup"}, {"type": "open", "screen": "knows"}]),
    ("open decisions", [{"type": "open", "screen": "decisions"}]), ("show me the team", [{"type": "open", "screen": "decisions"}]),
    ("open approvals", [{"type": "open", "screen": "decisions"}]),
    ("open the roundtable", [{"type": "open", "screen": "roundtable"}]), ("open the scorecard", [{"type": "open", "screen": "scorecard"}]),
    ("open server settings", [{"type": "navigate", "tab": "setup"}, {"type": "open", "screen": "server"}]),
    ("open notification settings", [{"type": "navigate", "tab": "setup"}, {"type": "open", "screen": "push"}]),
    ("open the coach chat", [{"type": "navigate", "tab": "coach"}]), ("take me to coach", [{"type": "navigate", "tab": "coach"}]),
    ("open today", [{"type": "navigate", "tab": "today"}]), ("show me my workout plan", [{"type": "navigate", "tab": "today"}]),
    ("open my review", [{"type": "open", "screen": "review"}]),
]


@pytest.mark.parametrize("phrase,actions", NAV)
def test_navigation_table(client, phrase, actions):
    j = say(client, "hey coach, " + phrase)
    assert j["intent"] == "navigate" and j["actions"] == actions, j


def test_navigation_to_a_day_of_meals_and_close(client):
    y = (date.today() - timedelta(days=1)).isoformat()
    j = say(client, "show me yesterday's meals")
    assert j["actions"] == [{"type": "navigate", "tab": "nutrition"}, {"type": "nutrition_day", "day": y}]
    assert say(client, "go back")["actions"] == [{"type": "close"}]
    assert say(client, "close this")["actions"] == [{"type": "close"}]


# ---------------------------------------------------------------- settings, rest
def test_settings(client):
    assert say(client, "turn hey coach off")["actions"] == [{"type": "setting", "key": "wake", "value": False}]
    assert say(client, "turn on hey coach")["actions"] == [{"type": "setting", "key": "wake", "value": True}]
    assert say(client, "turn off vibration")["actions"] == [{"type": "setting", "key": "vibration", "value": False}]
    j = say(client, "change my coach to mentor")
    assert j["actions"] == [{"type": "setting", "key": "persona", "value": "mentor"}] and db.get_profile()["coach_persona"] == "mentor"
    j = say(client, "switch my coach to Max")
    assert db.get_profile()["coach_persona"] == "hype"
    assert say(client, "change my coach to banana")["actions"] == []


def test_rest_commands(client):
    assert say(client, "rest plus 30 seconds")["actions"] == [{"type": "rest_add", "seconds": 30}]
    assert say(client, "add 30 seconds")["actions"] == [{"type": "rest_add", "seconds": 30}]
    assert say(client, "skip rest")["actions"] == [{"type": "rest_skip"}]
    assert say(client, "rest 90 seconds")["actions"] == [{"type": "rest", "seconds": 90}]


# ---------------------------------------------------------------- workout via live
@pytest.mark.parametrize("phrase", ["let's start today's workout", "start my session", "start the workout"])
def test_start_opens_live(client, phrase):
    j = say(client, phrase)
    assert j["intent"] == "start" and j["actions"][0] == {"type": "open", "screen": "live"} and j["state"]


def test_set_logging_and_missing_numbers_via_live(client):
    say(client, "start my session")
    j = say(client, "log this set")
    assert j["expects"] == "set_details"
    j = say(client, "80 for 8", followup=True)
    assert j["intent"] == "set" and any(a["type"] == "rest" for a in j["actions"]) and j["saved"]
    assert say(client, "what's next")["intent"] == "status"
    assert say(client, "skip this exercise")["intent"] == "skip"
    e = say(client, "wrap it up")
    assert e["intent"] == "end" and {"type": "end"} in e["actions"]


def test_live_allow_chat_false_never_calls_model(client, monkeypatch):
    monkeypatch.setattr(live, "have_key", lambda: True)
    monkeypatch.setattr(live.orch, "chat", lambda *a, **k: (_ for _ in ()).throw(AssertionError("chat called")))
    assert live.handle("tell me a story about dragons", allow_chat=False)["intent"] == "fallback"


# ---------------------------------------------------------------- safety, Hindi, offline fallback
def test_care_first_and_pain(client):
    j = say(client, "I want to die")
    assert j["intent"] == "care" and j["safety_flag"] == "crisis"
    j = say(client, "I'm skipping meals to get thin")
    assert j["intent"] == "care" and meals() == []
    j = say(client, "my knee really hurts on squats")
    assert j["intent"] == "pain" and j["safety_flag"]


def test_hindi_phrases(client):
    j = say(client, "मैंने दो अंडे खाए")
    assert j["intent"] in ("meal", "ask_food_amount"), j
    j = say(client, "हे कोच प्रोफाइल खोलो")
    assert j["intent"] in ("navigate", "fallback")


def test_offline_fallback_is_helpful(client):
    j = say(client, "what is the meaning of life")
    assert j["intent"] == "fallback" and "I ate" in j["reply"] and "Say it like" not in j["reply"] and j["actions"] == []


def test_noop_and_wake_only(client):
    assert say(client, "")["intent"] == "noop"
    assert say(client, "hey coach")["reply"]


# ---------------------------------------------------------------- confirm flow + TTL
def test_confirm_yes_no_and_ttl(client, monkeypatch):
    prof = lambda: db.get_profile()
    r = vc.t_change_profile_field("goal", "bulk")
    assert r["status"] == "needs_confirm" and prof()["goal"] != "bulk"
    j = say(client, "no thanks never mind")
    assert j["intent"] == "cancelled" and prof()["goal"] != "bulk"
    vc.t_change_profile_field("goal", "bulk")
    j = say(client, "yes do it")
    assert prof()["goal"] == "bulk" and {"type": "refresh", "what": ["plan", "today", "profile", "decisions"]} in j["actions"]
    vc.t_change_profile_field("goal", "cut")
    vc._pend["ts"] -= vc.PEND_TTL + 1       # expired: a late "yes" must do nothing
    say(client, "yes")
    assert prof()["goal"] == "bulk"


def test_non_destructive_profile_weight_is_direct(client):
    r = vc.t_change_profile_field("weight", "80.5")
    assert r["status"] == "applied" and db.get_profile()["weight_kg"] == 80.5


def test_profile_days_validation(client):
    assert vc.t_change_profile_field("days_per_week", "9")["status"] == "invalid"
    assert vc.t_change_profile_field("days_per_week", "4")["status"] == "needs_confirm"
    assert vc.t_change_profile_field("equipment", "bands")["status"] == "invalid"


# ---------------------------------------------------------------- LLM router with a fake run_agent
def fake_agent(calls):
    """calls: list of (tool, kwargs); run them through the handlers like the real loop and return 'reply'."""
    def run(system, user, tools, handlers, max_turns=6, max_tokens=700, history=None, trace=None, fast=False):
        assert "TODAY is" in system
        for name, kw in calls:
            assert name in {t["name"] for t in tools}
            res = handlers[name](**kw)
            if trace is not None:
                trace.append({"tool": name, "args": kw, "result": res})
        return "Okay."
    return run


def router(monkeypatch, calls):
    monkeypatch.setattr(vc, "have_key", lambda: True)
    monkeypatch.setattr(vc, "run_agent", fake_agent(calls))


def test_router_swap_exercise_validated(client, monkeypatch):
    st = live.session_state()
    exn = st["exercises"][0]["name"]
    router(monkeypatch, [("swap_exercise", {"exercise": exn, "replacement": "definitely not a real exercise"})])
    j = say(client, "swap that for something else")
    assert j["intent"] == "router" and j["actions"] == []        # invalid swap: nothing changed, no refresh
    router(monkeypatch, [("swap_exercise", {"exercise": exn, "scope": "today"})])
    j = say(client, "swap that for something else")
    assert {"type": "refresh", "what": ["plan", "today"]} in j["actions"]
    assert db.get_plan().get("today_override")


def test_router_agenda_event_validated(client, monkeypatch):
    start = (date.today() + timedelta(days=10)).isoformat()
    router(monkeypatch, [("add_agenda_event", {"kind": "competition", "start": start, "label": "meet"})])
    say(client, "I have a meet in ten days")
    from fitness_os import agenda
    assert any(e["kind"] == "competition" and e["start"] == start for e in agenda.upcoming(60))
    assert vc.t_add_agenda_event("busy", "2026-13-45")["status"] == "invalid"
    assert vc.t_add_agenda_event("nonsense", start)["status"] == "invalid"
    assert vc.t_add_agenda_event("travel", start, start, "trip")["status"] == "applied"


def test_router_checkin(client, monkeypatch):
    router(monkeypatch, [("log_checkin", {"sleep_hours": 6.5, "soreness": 4, "energy": 2, "stress": 5, "note": "rough week"})])
    j = say(client, "I slept six and a half hours, sore, low energy, stressed")
    assert {"type": "refresh", "what": ["today"]} in j["actions"]
    c = db.get_checkins(1)[0] if hasattr(db, "get_checkins") else None
    if c:
        assert c["sleep_h"] == 6.5 and c["soreness"] == 4


def test_router_profile_change_requires_confirm_then_applies(client, monkeypatch):
    router(monkeypatch, [("change_profile_field", {"field": "days_per_week", "value": "4"})])
    j = say(client, "make it four days a week")
    assert j["expects"] == "confirm" and "4 days" in j["reply"] or "4 days" in vc._pend["say"]
    assert vc._pend["op"] == "profile_apply"
    before = db.get_profile()["days_per_week"]
    j = say(client, "yeah")
    assert db.get_profile()["days_per_week"] == 4 and j["intent"] == "profile_apply"


def test_router_forget_requires_confirm(client, monkeypatch):
    from fitness_os.mem import ingest, store
    ingest.observe("I hate lunges", source="chat", channel="chat", explicit=True)
    facts = store.list_facts() if hasattr(store, "list_facts") else []
    called = {}
    from fitness_os import coach_tools
    monkeypatch.setattr(coach_tools, "forget_this", lambda fact_id: called.setdefault("id", fact_id) and {"status": "applied"})
    router(monkeypatch, [("forget_this", {"fact_id": 7})])
    j = say(client, "forget that I hate lunges")
    assert j["expects"] == "confirm" and not called
    say(client, "yes")
    assert called == {"id": 7}


def test_router_decision_approve_reads_summary_and_confirms(client, monkeypatch):
    from fitness_os import control, ledger
    from fitness_os.schema import Proposal
    monkeypatch.setattr(control, "approve", lambda i: {"id": i, "status": "applied"})
    monkeypatch.setattr(ledger, "get", lambda i: {"id": i, "status": "pending_approval", "proposal": Proposal(agent="training", kind="note", params={"text": "Add a deload week"}, rationale="fatigue", evidence="x", confidence=0.7).model_dump(),
                                                  "ts": "t", "agent": "training", "kind": "note", "rationale": "fatigue", "evidence": "x", "tier": "approval", "fired": [], "veto_until": None, "trigger_event": "t", "undo": None, "action": None})
    router(monkeypatch, [("approve_decision", {"decision_id": 5})])
    j = say(client, "approve the deload")
    assert j["expects"] == "confirm" and "?" in j["reply"]
    j = say(client, "confirm")
    assert j["intent"] == "approve" and "Approved" in j["reply"]


def test_router_evidence_and_failure_fallback(client, monkeypatch):
    router(monkeypatch, [("ask_evidence", {"question": "how much protein for muscle gain"})])
    assert say(client, "how much protein do I need for muscle")["intent"] == "router"
    monkeypatch.setattr(vc, "run_agent", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    j = say(client, "what is the meaning of life")
    assert j["intent"] == "fallback" and "I ate" in j["reply"]


def test_router_ambient_unaddressed_ignored(client, monkeypatch):
    router(monkeypatch, [])
    assert say(client, "this gym is so crowded today", ambient=True)["intent"] == "ignored"


def test_valid_action_helper_rejects_unknown():
    assert not vc.valid_action({"type": "teleport"}) and not vc.valid_action({"type": "open", "screen": "nope"})
    assert not vc.valid_action({"type": "rest", "seconds": 5000}) and vc.valid_action({"type": "setting", "key": "wake", "value": True})


def test_parse_day():
    t = date(2026, 11, 21)    # a Saturday
    assert vc.parse_day("two eggs yesterday", t) == ("2026-11-20", "two eggs")
    assert vc.parse_day("rice on monday", t)[0] == "2026-11-16"
    assert vc.parse_day("rice on saturday", t)[0] == "2026-11-21"
    assert vc.parse_day("rice", t)[0] is None


# ---------------------------------------------------------------- regressions: pending dialogues must not swallow new commands
@pytest.mark.parametrize("new", ["rebuild my plan for 5 days", "hello how are you", "open my calendar", "turn off vibration", "I ate two eggs"])
def test_pending_set_details_does_not_swallow_new_command(client, new):
    say(client, "start my session")
    assert say(client, "log this set")["expects"] == "set_details"
    j = say(client, new)
    assert j["expects"] != "set_details" and "How heavy" not in j["reply"] and "Got 5" not in j["reply"] and "missed that" not in j["reply"]
    assert not live._pending_get()
    if new == "open my calendar":
        assert j["intent"] == "navigate"


@pytest.mark.parametrize("ans", ["80", "8 reps", "80 for 8", "eighty kilos", "80 kg for 8 reps"])
def test_pending_set_details_still_accepts_set_answers(client, ans):
    say(client, "start my session")
    say(client, "log this set")
    j = say(client, ans)
    assert j["intent"] in ("set", "ask"), j


def test_pending_set_details_followup_true_keeps_old_behaviour(client):
    say(client, "start my session")
    say(client, "log this set")
    j = say(client, "hmm", followup=True)
    assert j["expects"] == "set_details"


def test_pending_set_details_voice_ttl(client):
    say(client, "start my session")
    say(client, "log this set")
    live._pending["ts"] -= vc.SET_PEND_TTL + 1
    assert say(client, "80", followup=True)["intent"] != "set"


def test_food_amount_question_cancelled_by_different_command(client):
    for new in ("open my calendar", "turn off vibration", "start my session", "I ate two bananas"):
        vc.reset()
        say(client, "I had eggs")
        j = say(client, new)
        assert j["expects"] != "food_amount" and not meals() or new == "I ate two bananas"


@pytest.mark.parametrize("s", ["I had a great day at work", "I had a meeting", "I had a bad night", "I had a great time"])
def test_non_food_had_sentences(client, s, monkeypatch):
    j = say(client, s)
    assert "don't know" not in j["reply"] and j["intent"] != "meal_unknown" and meals() == []
    assert j["intent"] == "meal_unclear"
    router(monkeypatch, [])               # with a key it falls through to the model instead
    assert say(client, s)["intent"] == "router"


def test_ambient_non_food_had_is_ignored(client):
    j = say(client, "I had a great day at work", ambient=True)
    assert j["intent"] == "ignored" and j["reply"] == "" and meals() == []


# ------------------------------------------------------------------ the diet consultant by voice
def test_open_the_diet_coach(client):
    for phrase in ("open my diet coach", "open the dietitian", "show me my meal plan", "go to diet plan"):
        r = say(client, phrase)
        assert r["intent"] == "navigate", phrase
        assert {"type": "navigate", "tab": "nutrition"} in r["actions"] and {"type": "open", "screen": "diet"} in r["actions"], phrase
    assert say(client, "open fuel")["actions"] == [{"type": "navigate", "tab": "nutrition"}]      # plain Fuel is unchanged


def test_diet_questions_go_to_the_dietitian_and_plans_open_the_screen(client, monkeypatch):
    from backend.diet import agent as diet_agent
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    seen = []

    def fake_chat(msg, fast=False):
        seen.append(msg)
        return {"reply": "Here is your day. Protein lands near 140 g.", "actions": [], "plan": {"meals": []}, "week": None, "needs": []}

    monkeypatch.setattr(diet_agent, "chat", fake_chat)
    for q in ("what should I eat after my workout", "give me a meal plan", "how much protein in paneer", "hey coach what should I have for dinner"):
        r = say(client, q)
        assert r["intent"] == "diet", q
        assert r["reply"].startswith("Here is your day")
        assert {"type": "open", "screen": "diet"} in r["actions"]
    assert len(seen) == 4


def test_logging_food_still_logs_and_does_not_go_to_the_dietitian(client, monkeypatch):
    from backend.diet import agent as diet_agent
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setattr(diet_agent, "chat", lambda m, fast=False: pytest.fail("a food log must not reach the dietitian"))
    r = say(client, "I ate 4 eggs", screen="nutrition")
    assert r["intent"] in ("food", "log_food", "meal") or "egg" in r["reply"].lower() or r["expects"] == "food_amount"


def test_diet_questions_without_a_key_fall_back_honestly(client):
    r = say(client, "what should I eat today")
    assert r["intent"] == "fallback" and r["reply"]


def test_the_router_remembers_its_own_clarifying_question(client, monkeypatch):
    """'three' -> 'What weight?' -> 'sixty' must reach the model together, not as a lone 'sixty'."""
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    seen = []

    def fake(system, user, tools, handlers, max_turns=6, max_tokens=1024, history=None, trace=None, fast=False):
        seen.append((user, list(history or [])))
        return "What weight did you use?" if len(seen) == 1 else "Logged."

    monkeypatch.setattr(vc, "run_agent", fake)
    say(client, "what do I do with three")
    say(client, "sixty", followup=True)
    assert len(seen) == 2 and seen[0][1] == []
    assert [h["role"] for h in seen[1][1]] == ["user", "assistant"] and "What weight" in seen[1][1][1]["content"]


def test_how_much_protein_is_answered_from_the_app_without_a_model(client, monkeypatch):
    monkeypatch.setattr(vc, "run_agent", lambda *a, **k: pytest.fail("a numbers question must not reach the model"))
    for q in ("then tell me how much protein", "how much protein", "what's my protein today", "how many calories do I have"):
        r = say(client, q)
        assert r["intent"] == "meal_left" and "protein" in r["reply"], q
