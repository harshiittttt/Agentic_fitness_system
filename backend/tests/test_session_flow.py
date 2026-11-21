"""Memory + consistency of the coach: which split day every session was, what finishing a session writes, and that live, chat
and the Today screen agree. Fully offline (the post-workout loop and the model are faked)."""
import copy
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from backend.agents import orchestrator as orch
from backend.coach import live, session_flow as sf
from backend.main import app
from backend.memory import db
from backend.tools import demo
from backend.tools import exercises as ex
from backend.tools import plan_edit as pe
from fitness_os import coach_context, memory


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    orch.onboard({**demo.PROFILE, "name": "Alex", "days_per_week": 4})
    real = db.add_set
    state = {"fresh": False}

    def aged(session, exercise, weight, reps, rpe=None, note=None):   # sets of a past label get an old timestamp (not "5 minutes ago")
        i = real(session, exercise, weight, reps, rpe, note)
        if not state["fresh"] and session.startswith("20"):
            with db.conn() as c:
                c.execute("UPDATE sets SET ts = ? WHERE id = ?", (session + "T12:00:00+00:00", i))
        return i
    monkeypatch.setattr(db, "add_set", aged)
    monkeypatch.setattr(db, "_state", state, raising=False)
    return tmp_path


def day_of(i: int) -> str:
    return (date(2026, 3, 1) + timedelta(days=i)).isoformat()


def train(label: str, day_idx: int | None = None, plan: dict | None = None):
    plan = plan or db.get_plan()
    d = plan["days"][day_idx if day_idx is not None else pe.plan_view()["today_index"]]
    for e in d["exercises"][:2]:
        db.add_set(label, e["name"], 60, e["reps"], 8)
    orch.log_and_coach(label, "") if False else None


def names(plan=None):
    return [d["name"] for d in (plan or db.get_plan())["days"]]


def next_name():
    v = pe.plan_view()
    return v["days"][v["today_index"]]["name"]


def set_today(monkeypatch, label):
    monkeypatch.setattr(ex, "today", lambda: label)


# ------------------------------------------------------------------ A. rotation
def test_rotation_walks_the_split_over_many_sessions(env, monkeypatch):
    n = names()
    for i in range(10):
        set_today(monkeypatch, day_of(i))
        assert next_name() == n[i % 4], i             # the day shown before training is the right one
        train(day_of(i))
        assert next_name() == n[i % 4]                # still that day while/after it was trained today
    set_today(monkeypatch, day_of(10))
    assert next_name() == n[10 % 4]


def test_stray_labels_skipped_days_and_two_sessions_in_a_day_do_not_move_it(env, monkeypatch):
    n = names()
    set_today(monkeypatch, day_of(0)); train(day_of(0))
    db.add_set("test", "squat", 50, 5, 8)             # stray non-date labels
    db.add_set("s", "bench press", 50, 5, 8)
    set_today(monkeypatch, day_of(1))
    assert next_name() == n[1]
    db.add_set(day_of(0), "curls", 20, 10, 8)         # an extra set on day 0 (a second visit the same day)
    assert next_name() == n[1]
    set_today(monkeypatch, day_of(6))                 # five rest days: the next session is still the next day of the split
    assert next_name() == n[1]
    db.add_set("2020-01-01", "squat", 50, 5, 8)       # back-dated label never moves the rotation
    assert next_name() == n[1]
    train(day_of(6)); set_today(monkeypatch, day_of(7))
    assert next_name() == n[2]


def test_split_change_4_to_5_continues_sensibly(env, monkeypatch):
    old = db.get_plan()
    n = names(old)
    for i in range(2):                                  # trained days 0 and 1 (Upper A, Lower A)
        set_today(monkeypatch, day_of(i)); train(day_of(i))
    set_today(monkeypatch, day_of(2))
    new = orch.onboard({**demo.PROFILE, "name": "Alex", "days_per_week": 5})    # rebuild to a 5 day split (Push/Pull/Legs/Upper/Lower)
    new = db.get_plan()
    assert len(new["days"]) == 5
    # "Lower A" no longer exists by name: closest exercise match decides, never an arbitrary jump
    idx = pe.plan_view()["today_index"]
    assert 0 <= idx < 5
    last_day_exs = {e["name"] for e in old["days"][1]["exercises"]}
    jac = lambda k: len(last_day_exs & {e["name"] for e in new["days"][k]["exercises"]}) / len(last_day_exs | {e["name"] for e in new["days"][k]["exercises"]})
    best = max(range(5), key=lambda k: (jac(k), -k))
    assert idx == (best + 1) % 5
    # and it keeps advancing from there
    train(day_of(2)); set_today(monkeypatch, day_of(3))
    assert pe.plan_view()["today_index"] == (idx + 1) % 5


def test_split_change_by_renaming_and_reordering_keeps_the_next_day(env, monkeypatch):
    plan = db.get_plan()
    n = names(plan)
    set_today(monkeypatch, day_of(0)); train(day_of(0))
    set_today(monkeypatch, day_of(1))
    assert next_name() == n[1]
    new = copy.deepcopy(plan); new["days"].reverse(); db.save_plan(new)    # reordered
    sf.on_split_changed(plan, new)
    pos = [d["name"] for d in new["days"]].index(n[0])
    assert next_name() == new["days"][(pos + 1) % 4]["name"]                # the day after the one trained, in the NEW order (matched by name)
    # a day that vanishes entirely and nothing like it exists -> day 1, not garbage
    only = copy.deepcopy(plan); only["days"] = [d for d in only["days"] if d["name"] != n[0]][:1]
    only["days"][0]["exercises"] = [{"name": "plank", "sets": 2, "reps": 30, "target_rpe": 7}]
    db.save_plan(only); sf.on_split_changed(new, only)
    assert pe.plan_view()["today_index"] == 0


def test_in_progress_session_survives_a_split_change(env, monkeypatch):
    plan = db.get_plan(); n = names(plan)
    set_today(monkeypatch, day_of(0)); train(day_of(0))       # trained today
    new = copy.deepcopy(plan); new["days"].insert(0, {"name": "Extra", "exercises": [{"name": "plank", "sets": 2, "reps": 30, "target_rpe": 7}]})
    db.save_plan(new); sf.on_split_changed(plan, new)
    v = pe.plan_view()
    assert v["days"][v["today_index"]]["name"] == n[0]        # today is still the day that was started
    assert len([s for s in db.get_sets(limit=50) if s["session"] == day_of(0)]) == 2


def test_early_morning_continuation_stays_one_session(env, monkeypatch):
    base = date(2026, 3, 1)
    db._state["fresh"] = True
    set_today(monkeypatch, base.isoformat()); train(base.isoformat())
    set_today(monkeypatch, (base + timedelta(days=1)).isoformat())    # it is now 00:30 the next day, the set below is 5 minutes later
    assert sf.current_label() == base.isoformat()                     # live keeps writing to the same session
    db.add_set(sf.current_label(), "curls", 20, 10, 8)
    assert sf.count() == 1 and next_name() == names()[0]


# ------------------------------------------------------------------ backward compat
def test_existing_database_with_sets_but_no_session_table(env, monkeypatch):
    with db.conn() as c:
        c.execute("DROP TABLE IF EXISTS training_session")
    for i in range(6):
        db.add_set(day_of(i), "squat", 100, 5, 8)
    set_today(monkeypatch, day_of(6))
    assert next_name() == names()[6 % 4]                    # same answer the old "count % days" rule gave
    assert pe.plan_view()["sessions_done"] == 6
    rows = sf.sessions()
    assert len(rows) == 6 and all(r["day_index"] is not None for r in rows)


def test_reset_history_clears_the_rotation(env, monkeypatch):
    set_today(monkeypatch, day_of(0)); train(day_of(0))
    set_today(monkeypatch, day_of(1)); assert pe.plan_view()["today_index"] == 1
    db.reset_history()
    assert pe.plan_view()["today_index"] == 0 and sf.count() == 0


# ------------------------------------------------------------------ C. finishing a session
def log_set(text):
    return live.handle(text)


def test_end_closes_writes_memory_targets_and_schedules_the_loop(env, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    calls = []
    from fitness_os import loop
    monkeypatch.setattr(loop, "run_event", lambda ev, payload=None, **kw: calls.append((ev, payload, kw)) or {})
    monkeypatch.setattr(live, "polish", lambda t, p, n: t)
    live.start()
    first = pe.plan_view()["days"][pe.plan_view()["today_index"]]["exercises"][0]["name"]
    log_set(f"{first} 80 for 8 rpe 7")
    log_set(f"{first} 80 for 8 rpe 7")
    r = live.handle("that's it, wrap up")
    sf.wait_background()
    assert r["intent"] == "end"
    row = sf.get(ex.today())
    assert row["status"] == "completed" and row["ended_ts"] and row["day_name"] == next_name()
    assert any("Trained 2 sets" in m["body"] for m in memory.items(kind="episodic"))          # episodic memory written now
    tg = db.get_plan()["next_targets"][first]
    assert tg["weight"] > 80                                                                  # easy (RPE 7): add load next time
    assert f"{first} {tg['weight']:g}x" in " ".join(sf.next_targets(db.get_plan(), row["day_index"]))
    assert [c[0] for c in calls] == ["session.logged"] and calls[0][2] == {"record": False}   # agent loop fired once, not re-recording
    assert calls[0][1]["sets"][0]["exercise"] == first
    live.handle("wrap up")                                                                    # ending twice: idempotent
    sf.wait_background()
    assert len(calls) == 1 and len([m for m in memory.items(kind="episodic") if "Trained 2 sets" in m["body"]]) == 1


def test_a_failing_model_or_loop_never_breaks_the_reply(env, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    from fitness_os import loop
    monkeypatch.setattr(loop, "run_event", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("429 quota")))
    monkeypatch.setattr(live, "polish", lambda t, p, n: t)
    first = pe.plan_view()["days"][pe.plan_view()["today_index"]]["exercises"][0]["name"]
    log_set(f"{first} 80 for 8 rpe 8")
    r = live.handle("i'm done")
    sf.wait_background()
    assert r["intent"] == "end" and r["reply"] and sf.get(ex.today())["status"] == "completed"


def test_pr_is_stored_in_memory(env, monkeypatch):
    for i in range(3):
        db.add_set(day_of(i), "squat", 100, 5, 8)
    set_today(monkeypatch, day_of(5))
    db.add_set(day_of(5), "squat", 120, 5, 8)
    res = sf.on_session_finished(day_of(5))
    assert res["prs"] == ["squat"]
    assert any("PR on squat: 120 x 5" in m["body"] for m in memory.items(kind="semantic"))


def test_abandoned_session_is_closed_by_the_safety_net(env, monkeypatch):
    first = pe.plan_view()["days"][0]["exercises"][0]["name"]
    set_today(monkeypatch, day_of(0))
    db._state["fresh"] = True
    db.add_set(day_of(0), first, 70, 8, 8)               # sets but nobody pressed end
    assert sf.close_stale() == []                         # just now: still going
    assert sf.get(day_of(0))["status"] == "active"
    monkeypatch.setenv("FITNESS_NOW", (__import__("datetime").datetime.now(__import__("datetime").timezone.utc) + timedelta(hours=6)).isoformat(timespec="seconds"))
    set_today(monkeypatch, day_of(1))
    from fitness_os import scheduler
    rep = scheduler.run_due()
    assert rep["closed_sessions"] == [day_of(0)]
    row = sf.get(day_of(0))
    assert row["status"] == "auto_closed" and row["result"]["auto"] is True
    assert any("Trained 1 sets" in m["body"] for m in memory.items(kind="episodic"))
    assert next_name() == names()[1]


# ------------------------------------------------------------------ D/F. live, chat and the Today screen agree
def test_live_chat_and_today_screen_agree(env, monkeypatch):
    client = TestClient(app)
    n = names()
    set_today(monkeypatch, day_of(0)); train(day_of(0))
    set_today(monkeypatch, day_of(1))
    idx = client.get("/plan").json()["today_index"]
    assert n[idx] == n[1]
    assert client.get("/insights/today").json()["next"]["name"] == n[1]            # Today screen card
    st = live.session_state()
    assert st["day"] == n[1] and st["day_index"] == idx                            # live session
    ctx = coach_context.build(live_context=live.live_context())                    # chat + live prompt context
    assert f"Next/today session: {n[1]} (day {idx + 1} of 4)" in ctx
    assert f"Live session, {n[1]} (day {idx + 1} of 4 in the split)" in ctx
    # live coach asks the model with channel='live' and the accurate context
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    seen = {}
    monkeypatch.setattr(orch, "chat", lambda msg, live_context="", channel="chat": seen.update(ctx=live_context, ch=channel) or "ok")
    r = live.handle("what should I think about on this lift")
    assert r["reply"] == "ok" and seen["ch"] == "live" and n[1] in seen["ctx"] and "sets left" in seen["ctx"]


def test_context_flags_mismatches_and_shows_learned_facts(env, monkeypatch):
    from fitness_os import conversation
    conversation.record("chat", "user", "I can only train 3 days a week")
    conversation.record("chat", "user", "I hate lunges")
    conversation.record("chat", "user", "my left knee gets sore on deep squats")
    ctx = coach_context.build()
    assert "can train 3 days per week" in ctx and "Dislikes: lunges" in ctx
    assert "says they can train 3 days a week but the split has 4" in ctx
    assert "left knee gets sore on deep squats" in ctx and len(ctx) < 4000
    assert "left knee" in " ".join(m["text"] for m in db.get_memory(20) if m["kind"] == "injury")    # planning sees the injury too
    assert "lunges" in db.get_profile()["avoid"]


def test_summary_endpoint_closes_a_finished_session_once(env):
    client = TestClient(app)
    today = pe.plan_view()["days"][pe.plan_view()["today_index"]]
    for e in today["exercises"]:
        for _ in range(e["sets"]):
            db.add_set(ex.today(), e["name"], 50, e["reps"], 8)
    assert client.get("/session/summary").json()["finished"] is True
    assert sf.get(ex.today())["status"] == "completed"
    client.get("/session/summary")
    assert len([m for m in memory.items(kind="episodic") if "Trained" in m["body"]]) == 1


def test_coach_memory_endpoint_lists_and_forgets(env):
    from fitness_os import conversation
    client = TestClient(app)
    conversation.record("chat", "user", "I hate burpees")
    body = client.get("/coach/memory").json()
    f = next(f for f in body["facts"] if "burpees" in f["body"])
    assert client.delete(f"/coach/memory/{f['id']}").json() == {"ok": True}
    assert not any("burpees" in f["body"] for f in client.get("/coach/memory").json()["facts"])
    assert client.delete("/coach/memory/999999").status_code == 404


# ------------------------------------------------------------------ skip_session advances the rotation
def _skip_today(reason="tired"):
    from fitness_os import user_ops
    user_ops._skip({"reason": reason}).apply()


def test_skip_session_advances_the_rotation(env, monkeypatch):
    n = names()
    set_today(monkeypatch, day_of(0)); train(day_of(0))
    set_today(monkeypatch, day_of(1))
    assert next_name() == n[1]
    _skip_today()
    assert next_name() == n[2]                         # the skipped day is gone, not "still next"
    set_today(monkeypatch, day_of(2))                  # next day: still n[2], the skip does not count twice
    assert next_name() == n[2]
    train(day_of(2))
    sf.sync()
    assert sf.get(day_of(2))["day_name"] == n[2]
    set_today(monkeypatch, day_of(3))
    assert next_name() == n[3]


def test_skip_twice_in_one_day_is_refused_and_two_skip_days_advance_twice(env, monkeypatch):
    from fitness_os import user_ops
    n = names()
    set_today(monkeypatch, day_of(0)); train(day_of(0))
    set_today(monkeypatch, day_of(1)); _skip_today()
    with pytest.raises(user_ops.OpError):
        _skip_today()
    set_today(monkeypatch, day_of(2))
    assert next_name() == n[2]
    _skip_today()
    assert next_name() == n[3]
    set_today(monkeypatch, day_of(3))
    assert next_name() == n[3]


def test_skip_then_train_the_same_day_trains_the_skipped_day(env, monkeypatch):
    n = names()
    set_today(monkeypatch, day_of(0)); train(day_of(0))
    set_today(monkeypatch, day_of(1)); _skip_today()
    train(day_of(1), 1)                                # changed their mind and trained the skipped day
    sf.sync()
    assert sf.get(day_of(1))["day_name"] == n[1]
