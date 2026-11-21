"""Memory layer, Curator, reflection loop and scheduler. Offline: a simulated clock (FITNESS_NOW) lets weeks pass instantly."""
import pytest

from backend.agents import orchestrator as orch
from backend.memory import db
from backend.tools import demo
from fitness_os import control, critic, curator, ledger, memory, reflection, runtime, scheduler
from fitness_os.schema import Proposal

T0 = "2026-11-02T09:00:00+00:00"  # a Monday


def at(monkeypatch, ts):
    monkeypatch.setenv("FITNESS_NOW", ts)


def plus(days, hh=9):
    from datetime import datetime, timedelta
    return (datetime.fromisoformat(T0) + timedelta(days=days)).replace(hour=hh).isoformat()


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    at(monkeypatch, T0)
    orch.onboard({**demo.PROFILE, "name": "Alex"})


# ---------------- memory store ----------------
def test_repeats_reinforce_instead_of_piling_up(monkeypatch):
    a = memory.add("procedural", "Responds well to higher training frequency", 0.5)
    b = memory.add("procedural", "Responds well to higher training frequency!", 0.5)
    assert b["merged"] and b["id"] == a["id"]
    it = memory.get(a["id"])
    assert it["hits"] == 2 and it["salience"] > 0.5
    assert not memory.add("procedural", "Knees hurt on deep squats", 0.5)["merged"]


def test_decay_archives_stale_memories_but_never_pinned_ones(monkeypatch):
    stale = memory.add("episodic", "Nov 2: trained 12 sets", 0.4)["id"]
    safe = memory.add("semantic", "Left knee hurts on deep squats", 0.4, pinned=True)["id"]
    at(monkeypatch, plus(200))
    assert memory.archive_stale() == 1
    assert memory.get(stale)["status"] == "archived" and memory.get(safe)["status"] == "active"
    assert [r["id"] for r in memory.recall("knee squats")] == [safe]


def test_recall_prefers_relevant_and_fresh_and_stays_small(monkeypatch):
    for i in range(12):
        memory.add("episodic", f"Day {i}: slept badly then trained legs hard {i * 7}", 0.5, min_sim=0.99)  # distinct days stay distinct
    hit = memory.add("procedural", "Shoulder pain flares with overhead pressing", 0.5)["id"]
    out = memory.recall("shoulder overhead press", k=3)
    assert len(out) == 3 and out[0]["id"] == hit


def test_empty_memory_is_rejected():
    with pytest.raises(ValueError):
        memory.add("semantic", "   ")


# ---------------- curator ----------------
def seed_day(monkeypatch, day_ts):
    at(monkeypatch, day_ts)
    for w, r in ((100, 5), (100, 5), (105, 3)):
        db.add_set("s", "squat", w, r, 8)
    db.add_checkin(5.5, 4, 2, 81.4, "legs are wrecked")
    db.add_meal(day_ts[:10], "chicken breast", 150, 250, 45, 0, 5)


def test_nightly_summary_is_deterministic_and_idempotent(monkeypatch):
    seed_day(monkeypatch, T0)
    at(monkeypatch, plus(1, 3))
    r1 = curator.run_nightly(day=T0[:10], use_llm=False)
    assert "Trained 3 sets" in r1["summary"]["text"] and "squat" in r1["summary"]["text"] and "slept 5.5h" in r1["summary"]["text"]
    r2 = curator.run_nightly(day=T0[:10], use_llm=False)
    assert r2["summary"]["merged"] and len(memory.items("episodic")) == 1


def test_a_quiet_day_stores_nothing(monkeypatch):
    assert curator.summarize_day("2026-10-01", use_llm=False) is None


def test_llm_rewrite_may_never_change_a_number(monkeypatch):
    seed_day(monkeypatch, T0)
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setattr(curator, "quick_text", lambda *a, **k: "You trained hard. Squat went 110x5 and you slept 6h.")  # invented numbers
    t1 = curator.summarize_day(T0[:10])["text"]
    assert "110x5" not in t1 and "Trained 3 sets" in t1
    good = curator.deterministic_summary(curator.day_facts(T0[:10]))
    monkeypatch.setattr(curator, "quick_text", lambda *a, **k: good.replace("Trained", "You trained"))
    memory_ids = {i["id"] for i in memory.items("episodic")}
    t2 = curator.summarize_day(T0[:10])["text"]
    assert t2.startswith("Nov 2: You trained") and memory_ids  # numbers identical, so the friendlier wording was accepted


def test_contradiction_between_stated_diet_and_logged_food_is_flagged_once(monkeypatch):
    db.add_memory("preference", "I am vegetarian")
    seed_day(monkeypatch, T0)
    f1 = curator.contradictions()
    assert len(f1) == 1 and "vegetarian" in f1[0]["text"] and "chicken" in f1[0]["text"]
    assert curator.contradictions()[0]["merged"]                     # a second night does not duplicate the question
    assert [i["status"] for i in memory.items(status="flagged")] == ["flagged"]


def test_dedupe_keeps_the_stronger_fact(monkeypatch):
    a = memory.add("semantic", "Prefers training in the morning before work", 0.6, min_sim=0.99)["id"]
    b = memory.add("semantic", "Prefers morning training before work", 0.5, min_sim=0.99)["id"]
    assert curator.dedupe() == 1
    assert {memory.get(a)["status"], memory.get(b)["status"]} == {"active", "archived"}


# ---------------- reflection ----------------
def checkin(monkeypatch, day, sleep, sore, energy, bw=None):
    at(monkeypatch, plus(day))
    db.add_checkin(sleep, sore, energy, bw, None)


def decide(monkeypatch, kind, frac, day=0):
    at(monkeypatch, plus(day))
    res = control.submit(Proposal(agent="training" if kind == "volume_change" else "nutrition", kind=kind, params={"fraction": frac},
                                  rationale="because the numbers said so", evidence="x"))
    assert res["status"] == "applied", res
    return res["id"]


def test_volume_cut_that_restored_readiness_is_learned_as_a_lesson(monkeypatch):
    for d in range(-10, 0):
        checkin(monkeypatch, d, 5.0, 4, 2)          # tired before
    did = decide(monkeypatch, "volume_change", -0.10)
    for d in range(1, 7):
        checkin(monkeypatch, d, 8.0, 2, 4)          # recovered after
    at(monkeypatch, plus(8))
    out = reflection.run()
    assert [(o["decision"], o["verdict"]) for o in out] == [(did, "helped")]
    lessons = memory.items("procedural")
    assert len(lessons) == 1 and "cutting volume 10%" in lessons[0]["body"] and lessons[0]["source_decision"] == did
    assert ledger.get(did)["outcome"]["reflected"] is True
    assert reflection.run() == []                    # never scored twice


def test_volume_increase_followed_by_worse_readiness_is_recorded_as_a_mistake(monkeypatch):
    for d in range(-10, 0):
        checkin(monkeypatch, d, 8.0, 2, 4)
    did = decide(monkeypatch, "volume_change", 0.05)
    for d in range(1, 7):
        checkin(monkeypatch, d, 5.0, 5, 1)
    at(monkeypatch, plus(8))
    out = reflection.run()
    assert out[0]["verdict"] == "hurt" and "backfired" in out[0]["lesson"]
    assert memory.items("procedural")[0]["salience"] >= 0.75


def test_thin_data_waits_then_is_recorded_honestly_without_inventing_a_lesson(monkeypatch):
    did = decide(monkeypatch, "volume_change", -0.10)
    at(monkeypatch, plus(8))
    assert reflection.run() == [] and "reflected" not in (ledger.get(did).get("outcome") or {})   # keep waiting
    at(monkeypatch, plus(30))
    out = reflection.run()
    assert out[0]["verdict"] == "insufficient_data" and memory.items("procedural") == []
    assert ledger.get(did)["outcome"]["reflected"] is True


def test_calorie_cut_is_judged_against_the_goal_direction(monkeypatch):
    db.save_profile({**db.get_profile(), "goal": "cut"})
    for d in range(-12, 0, 3):
        checkin(monkeypatch, d, 7.5, 3, 3, 82.0)    # flat before
    did = decide(monkeypatch, "calorie_change", -0.05)
    for i, d in enumerate(range(1, 15, 3)):
        checkin(monkeypatch, d, 7.5, 3, 3, 82.0 - 0.3 * i)   # drifting down after
    at(monkeypatch, plus(15))
    out = reflection.run()
    assert out[0]["decision"] == did and out[0]["verdict"] == "helped" and "right way" in out[0]["lesson"]


def test_repeated_lessons_reinforce_the_same_memory(monkeypatch):
    for round_ in range(2):
        base = round_ * 30
        for d in range(base - 10, base):
            checkin(monkeypatch, d, 5.0, 4, 2)
        decide(monkeypatch, "volume_change", -0.10, day=base)
        for d in range(base + 1, base + 7):
            checkin(monkeypatch, d, 8.0, 2, 4)
        at(monkeypatch, plus(base + 9))
        reflection.run()
        monkeypatch.setattr(ledger, "applied_volume_increase", lambda days=7: 0.0)
    lessons = memory.items("procedural")
    assert len(lessons) == 1 and lessons[0]["hits"] == 2


# ---------------- scheduler ----------------
def fake_agents(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    quiet = lambda s, u, t, max_tokens=0: ({"reasoning": "all fine", "proposals": []} if t["name"] == "submit_proposals" else {"assessment": "", "reviews": []})
    for mod in (runtime, critic):
        monkeypatch.setattr(mod, "extract_structured", quiet)


def test_last_occurrence_math():
    from datetime import datetime, timezone
    wed = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)
    assert scheduler.last_occurrence(wed, 5, 30, None).isoformat() == "2026-11-04T05:30:00+00:00"
    assert scheduler.last_occurrence(wed.replace(hour=4), 5, 30, None).isoformat() == "2026-11-03T05:30:00+00:00"
    assert scheduler.last_occurrence(wed, 18, 0, 6).isoformat() == "2026-11-01T18:00:00+00:00"      # last Sunday


def test_daily_runs_once_per_day_and_catches_up_after_being_off(monkeypatch):
    fake_agents(monkeypatch)
    at(monkeypatch, "2026-11-04T10:00:00+00:00")
    assert scheduler.is_due("cron.daily")
    r = scheduler.run_due(only="cron.daily")
    assert "cron.daily" in r["jobs"] and not scheduler.is_due("cron.daily")
    assert "cron.daily" not in scheduler.run_due()["jobs"]                  # a second tick the same day does nothing
    at(monkeypatch, "2026-11-05T05:29:00+00:00")
    assert not scheduler.is_due("cron.daily")                               # not yet 05:30
    at(monkeypatch, "2026-11-05T21:00:00+00:00")
    assert scheduler.is_due("cron.daily")                                   # the laptop was off at 05:30: catch up


def test_weekly_review_is_tied_to_sunday_evening(monkeypatch):
    fake_agents(monkeypatch)
    at(monkeypatch, "2026-11-04T10:00:00+00:00")                            # Wednesday
    assert scheduler.is_due("cron.weekly")                                  # last Sunday's review never ran
    scheduler.run_due(only="cron.weekly")
    assert not scheduler.is_due("cron.weekly")
    at(monkeypatch, "2026-11-08T17:59:00+00:00")
    assert not scheduler.is_due("cron.weekly")
    at(monkeypatch, "2026-11-08T18:01:00+00:00")
    assert scheduler.is_due("cron.weekly")


def test_without_a_model_the_daily_job_runs_offline_then_upgrades_once_a_key_exists(monkeypatch):
    at(monkeypatch, "2026-11-04T10:00:00+00:00")
    r = scheduler.run_due(only="cron.daily")["jobs"]["cron.daily"]
    assert r["mode"] == "offline" and "recovery" in r["skipped_llm_agents"] and "logistics" not in r["skipped_llm_agents"]
    assert not scheduler.is_due("cron.daily")                               # offline run counts for today: no hammering every tick
    assert scheduler.status()["jobs"]["cron.daily"]["mode"] == "offline"
    fake_agents(monkeypatch)                                                # a key appears the same day
    assert scheduler.is_due("cron.daily")                                   # the full LLM run is owed, once
    assert scheduler.run_due(only="cron.daily")["jobs"]["cron.daily"]["mode"] == "llm"
    assert not scheduler.is_due("cron.daily")


def test_a_failing_job_backs_off_and_does_not_stop_the_others(monkeypatch):
    at(monkeypatch, "2026-11-04T10:00:00+00:00")
    real = scheduler._run
    monkeypatch.setattr(scheduler, "_run", lambda name: (_ for _ in ()).throw(RuntimeError("boom")) if name == "curator" else real(name))
    r = scheduler.run_due()
    assert r["jobs"]["curator"]["error"] == "boom"
    assert not scheduler.is_due("curator")                                  # backing off
    at(monkeypatch, "2026-11-04T10:25:00+00:00")
    assert scheduler.is_due("curator")


def test_curator_job_runs_offline_and_status_reports_it(monkeypatch):
    seed_day(monkeypatch, "2026-11-03T09:00:00+00:00")
    at(monkeypatch, "2026-11-04T10:00:00+00:00")
    r = scheduler.run_due(only="curator")
    assert r["jobs"]["curator"]["summarised"] is True
    st = scheduler.status()
    assert st["jobs"]["curator"]["due"] is False and st["jobs"]["curator"]["last_result"]["summarised"] is True


def test_two_days_with_the_same_numbers_are_two_memories_not_one(monkeypatch):
    a = memory.add("episodic", "Nov 2: Trained 3 sets (top: squat 100x5 @8). Check-in: slept 7h.", 0.5, min_sim=0.98)
    b = memory.add("episodic", "Nov 3: Trained 3 sets (top: squat 100x5 @8). Check-in: slept 7h.", 0.5, min_sim=0.98)
    assert not b["merged"] and a["id"] != b["id"]


# ---------------- wiring: context pack, prompts, HTTP ----------------
def test_agents_receive_relevant_lessons_in_their_context_pack(monkeypatch):
    from fitness_os import context
    memory.add("procedural", "For this athlete, cutting volume 10% worked: readiness +9 over 7 days.", 0.7)
    for i in range(10):
        memory.add("episodic", f"Nov {i}: ate oats and trained pull day number {i * 11}", 0.5, min_sim=0.99)
    pk = context.pack("training", query="checkin.logged low readiness cut volume")
    assert len(pk["memories"]) <= 6
    assert pk["memories"][0]["kind"] == "procedural" and "cutting volume" in pk["memories"][0]["body"]


def test_prompts_tell_agents_how_to_treat_memories():
    from fitness_os import prompts
    for a in ("training", "nutrition", "recovery", "progress"):
        assert "context_pack.memories" in prompts.system_prompt(a)


def test_http_memory_and_scheduler_endpoints(monkeypatch):
    from fastapi.testclient import TestClient
    from backend.main import app
    c = TestClient(app)
    fid = memory.add("semantic", "You said you are vegetarian, but you logged chicken recently. Is that still true?", 0.9, status="flagged")["id"]
    assert [m["id"] for m in c.get("/agents/memory", params={"status": "flagged"}).json()] == [fid]
    assert c.post(f"/agents/memory/{fid}/resolve").json()["status"] == "resolved"
    assert c.get("/agents/memory", params={"status": "flagged"}).json() == []
    assert c.post("/agents/memory/9999/resolve").status_code == 404
    st = c.get("/agents/scheduler").json()
    assert set(st["jobs"]) == {"cron.daily", "cron.weekly", "curator", "proactive", "memory.nightly", "memory.weekly"}
    tick = c.post("/agents/scheduler/tick").json()
    assert "curator" in tick["jobs"] and tick["reflected"] == []        # no key: daily/weekly run in offline mode, curator runs offline
    assert c.post("/agents/scheduler/tick").json()["jobs"] == {}        # daily/weekly already ran offline for this moment: nothing owed until a key exists


def test_cli_memory_and_reflect_commands(monkeypatch, capsys):
    from fitness_os import cli
    memory.add("procedural", "Responds well to higher frequency", 0.6)
    cli.main(["memory", "list"])
    assert "Responds well to higher frequency" in capsys.readouterr().out
    cli.main(["reflect"])
    assert capsys.readouterr().out.strip() == "[]"
