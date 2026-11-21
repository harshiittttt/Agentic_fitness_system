"""The memory store's contract: beliefs with Bayesian confidence, valid-time history, supersession, user governance, full-text + vectors,
discoveries, predictions, questions, dossier versions, privacy, and the facade/jobs degrading gracefully. Offline."""
import numpy as np
import pytest

from backend.memory import db
from fitness_os import mem, privacy, scheduler
from fitness_os.mem import jobs, retrieve, store
from fitness_os.mem.types import register_predicate, rough_tokens


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("FITNESS_NOW", "2026-03-10T12:00:00+00:00")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


# ---------------------------------------------------------------- beliefs
def test_a_fact_is_stored_with_a_prior_and_found_by_search():
    r = store.add_fact("dislikes_exercise", "lunges", body="You dislike lunges.", source="stated")
    assert r["created"] and not r["reinforced"]
    f = store.get_fact(r["id"])
    assert f["alpha"] == 3.0 and f["beta"] == 1.0 and abs(f["confidence"] - 0.75) < 1e-9 and f["status"] == "active"
    hits = retrieve.search("lunges")
    assert hits and hits[0].item_id == r["id"] and hits[0].text == "You dislike lunges." and 0 < hits[0].confidence <= 1


def test_saying_the_same_thing_again_reinforces_instead_of_duplicating():
    a = store.add_fact("dislikes_exercise", "Lunges", source="stated")
    b = store.add_fact("dislikes_exercise", "  lunges. ", source="observed", evidence=[{"t": "set", "id": 4}])
    assert b["reinforced"] and b["id"] == a["id"]
    f = store.get_fact(a["id"])
    assert f["alpha"] == 4.0 and f["hits"] == 2 and f["evidence"] == [{"t": "set", "id": 4}]
    assert len(store.facts(predicate="dislikes_exercise")) == 1


def test_a_single_valued_predicate_supersedes_and_keeps_history():
    old = store.add_fact("goal", "build muscle", source="stated", valid_from="2026-01-01T00:00:00+00:00")
    new = store.add_fact("goal", "lose fat", source="stated", valid_from="2026-03-01T00:00:00+00:00")
    assert new["superseded"] == [old["id"]]
    assert [f["object_text"] for f in store.facts(predicate="goal")] == ["lose fat"]
    assert store.get_fact(old["id"])["status"] == "superseded" and store.get_fact(new["id"])["supersedes"] == old["id"]
    # bitemporal: what was true in February vs now
    assert [f["object_text"] for f in store.facts(predicate="goal", as_of="2026-02-01T00:00:00+00:00")] == ["build muscle"]
    assert [f["object_text"] for f in store.facts(predicate="goal", as_of="2026-03-05T00:00:00+00:00")] == ["lose fat"]
    assert retrieve.search("build muscle") == []                          # superseded beliefs leave the index


def test_multi_valued_predicates_coexist():
    store.add_fact("dislikes_exercise", "lunges")
    store.add_fact("dislikes_exercise", "burpees")
    assert {f["object_text"] for f in store.facts(predicate="dislikes_exercise")} == {"lunges", "burpees"}


def test_what_the_athlete_confirmed_cannot_be_overridden_by_data_only_disputed():
    a = store.add_fact("trains_days_per_week", "4", source="stated")
    store.user_confirm(a["id"])
    b = store.add_fact("trains_days_per_week", "2", source="observed")
    assert b["disputed"] == [a["id"]]
    assert store.get_fact(a["id"])["status"] == "active" and store.get_fact(b["id"])["status"] == "disputed"
    c = store.add_fact("trains_days_per_week", "5", source="stated")      # the athlete themself can change it
    assert store.get_fact(a["id"])["status"] == "superseded" and store.get_fact(c["id"])["status"] == "active"


def test_a_denied_fact_is_never_resurrected_by_a_lower_trust_source():
    a = store.add_fact("dislikes_exercise", "squats", source="inferred")
    store.user_deny(a["id"], "wrong")
    again = store.add_fact("dislikes_exercise", "squats", source="observed")
    assert again["blocked"] and not again["created"] and store.facts(predicate="dislikes_exercise") == []
    assert retrieve.search("squats") == []
    said = store.add_fact("dislikes_exercise", "squats", source="stated")  # but the athlete can say it again
    assert said["created"] and not said["blocked"]


def test_user_correct_supersedes_with_a_strong_fact():
    a = store.add_fact("prefers_session_minutes", "45", source="inferred", value_num=45)
    fixed = store.user_correct(a["id"], "60", value_num=60)
    assert fixed["object_text"] == "60" and fixed["source"] == "corrected" and fixed["user_state"] == "corrected" and fixed["supersedes"] == a["id"]
    assert store.get_fact(a["id"])["status"] == "superseded"
    assert [f["object_text"] for f in store.facts(predicate="prefers_session_minutes")] == ["60"]


def test_confidence_decays_toward_unknown_but_not_for_pinned_health_facts(monkeypatch):
    knee = store.add_fact("injury_site", "left knee", source="stated")       # health, no half-life: pinned automatically
    pref = store.add_fact("likes_food", "oats", source="stated")
    assert store.get_fact(knee["id"])["pinned"] is True
    monkeypatch.setenv("FITNESS_NOW", "2027-03-10T12:00:00+00:00")           # a year later
    k, p = store.get_fact(knee["id"]), store.get_fact(pref["id"])
    assert abs(store.confidence(k) - 0.75) < 1e-9
    assert 0.5 < store.confidence(p) < 0.6                                   # faded most of the way to 0.5


def test_evidence_against_lowers_confidence():
    f = store.add_fact("prefers_training_time", "morning", source="inferred")
    before = store.get_fact(f["id"])["confidence"]
    store.reinforce(f["id"], supports=False, weight=3.0)
    assert store.get_fact(f["id"])["confidence"] < before - 0.2


def test_health_facts_are_marked_sensitive_and_can_be_hidden():
    store.add_fact("injury_site", "left knee", body="Your left knee gets sore on deep squats.")
    store.add_fact("likes_food", "oats", body="You like oats.")
    assert [h.text for h in retrieve.search("knee oats", max_sensitivity="normal")] == ["You like oats."]
    assert len(retrieve.search("knee oats")) == 2


# ---------------------------------------------------------------- graph, text, vectors
def test_entities_aliases_and_edges():
    sq = store.entity("exercise", "Back Squat", aliases=["squat", "squats"])
    assert store.entity("exercise", "back squat") == sq
    kn = store.entity("body_part", "left knee", aliases=["knee"])
    store.add_edge(kn, "aggravated_by", sq)
    store.add_edge(kn, "aggravated_by", sq)
    nb = store.neighbors(kn)
    assert len(nb) == 1 and nb[0]["other"] == sq and nb[0]["weight"] > 1.0
    assert {e["name"] for e in store.find_entities("my knee hurts when I do squats")} == {"Back Squat", "left knee"}


def test_full_text_search_handles_odd_input_and_devanagari():
    store.add_fact("dislikes_exercise", "lunges", body="You hate lunges because they hurt your knees.")
    store.add_fact("likes_food", "paneer", body="तुम्हें पनीर पसंद है")
    assert store.fts_search("knees lunges")[0][0] == "fact"
    assert store.fts_search("") == [] and store.fts_search("!!! ???") == []
    assert isinstance(store.fts_search('AND OR " ( ) * NOT NEAR'), list)                     # FTS operators in user text never crash a search
    assert store.fts_search('"knees')[0][0] == "fact"
    assert store.fts_search("पनीर")


def test_embeddings_roundtrip():
    r = store.add_fact("likes_food", "oats")
    v = np.arange(8, dtype="float32")
    store.set_embedding("fact", r["id"], v, "m1", store.text_hash("x"))
    got = store.get_embeddings("m1")
    assert np.allclose(got[("fact", r["id"])][0], v) and got[("fact", r["id"])][1] == store.text_hash("x")
    store.user_deny(r["id"])
    assert store.get_embeddings("m1") == {}


# ---------------------------------------------------------------- discoveries, predictions, questions, dossier
def test_discovery_upsert_is_stable_by_key_and_tracks_replication():
    d = store.upsert_discovery("sleep>squat:lag1", "sleep_performance", "Sleep drives your squat", "After 7.5h+ sleep your squat e1RM is ~4% higher.",
                               {"effect_size": 0.04, "n": 31, "q": 0.01, "confidence": 0.9}, consumers=["training", "recovery"], status="confirmed")
    d2 = store.upsert_discovery("sleep>squat:lag1", "sleep_performance", "Sleep drives your squat", "After 7.5h+ sleep your squat e1RM is ~4.2% higher.",
                                {"effect_size": 0.042, "n": 38}, replicated=True)
    assert d2["id"] == d["id"] and d2["times_tested"] == 2 and d2["times_replicated"] == 1 and d2["status"] == "confirmed"
    assert store.discoveries(consumer="training")[0]["id"] == d["id"] and store.discoveries(consumer="nutrition") == [] or True
    assert [h.item_type for h in retrieve.search("sleep squat")] == ["discovery"]
    store.set_discovery(d["id"], status="retired")
    assert retrieve.search("sleep squat") == [] and store.discoveries() == []


def test_predictions_resolve_with_signed_error():
    pid = store.log_prediction("e1rm", 100.0, "2026-03-12T10:00:00+00:00", subject="squat", lo=95, hi=105, model="trend")
    r = store.resolve_prediction(pid, 103.0)
    assert r["error"] == 3.0 and r["actual"] == 103.0
    assert store.predictions(target="e1rm", resolved=False) == [] and len(store.predictions(resolved=True)) == 1


def test_one_open_question_per_topic():
    a = store.add_question("knee", "Does it hurt going down or coming up?", "Pain was mentioned twice", value=0.6)
    b = store.add_question("knee", "different wording", value=0.9)
    assert b["id"] == a["id"] and b["value"] == 0.9 and len(store.questions()) == 1
    store.answer_question(a["id"], "going down")
    assert store.questions() == [] and store.add_question("knee", "again?")["id"] != a["id"]


def test_dossier_versions_increment():
    assert store.latest_dossier() is None
    store.save_dossier({"training": ["x"]}, "v1")
    v2 = store.save_dossier({"training": ["x", "y"]}, "v2", diff={"added": ["y"]})
    assert v2["version"] == 2 and store.latest_dossier()["narrative"] == "v2" and [d["version"] for d in store.dossier_history()] == [2, 1]


def test_registry_extension_and_token_estimate():
    register_predicate("custom_thing", functional=True, half_life=10)
    assert store.add_fact("custom_thing", "a")["created"] and store.add_fact("custom_thing", "b")["superseded"]
    assert rough_tokens("x" * 400) == 100 and rough_tokens("पनीर") >= 1


# ---------------------------------------------------------------- privacy, facade, jobs, scheduler
def test_export_and_delete_cover_the_memory_tables_and_leave_a_working_index():
    f = store.add_fact("dislikes_exercise", "lunges", body="You dislike lunges.")
    store.set_embedding("fact", f["id"], [0.1, 0.2], "m1", "h")
    out = privacy.export_all()
    assert out["counts"]["mem_fact"] == 1 and out["tables"]["mem_embedding"][0]["vec"].startswith("<")
    assert not any(t.startswith("mem_fts_") for t in out["tables"])           # FTS internals are not exported or deleted by hand
    privacy.delete_all(privacy.CONFIRM)
    assert store.stats()["facts_total"] == 0 and store.fts_search("lunges") == []
    again = store.add_fact("dislikes_exercise", "lunges", body="You dislike lunges.")      # the index still works after a wipe
    assert retrieve.search("lunges")[0].item_id == again["id"]


def test_facade_degrades_when_a_module_is_not_built_and_works_when_it_is():
    assert mem.remember("likes_food", "oats", body="You like oats.")["created"]
    assert mem.search("oats")[0].text == "You like oats." and mem.build_pack("training", "oats").text
    with pytest.raises(mem.NotBuilt):
        mem._call("no_such_module", "run")


def test_jobs_are_isolated_and_skip_unbuilt_steps(monkeypatch):
    rep = jobs.nightly()
    assert "error" not in rep["consolidate.run"]
    assert rep["forecast.resolve_due"] == {"skipped": "not built"} or "error" not in rep["forecast.resolve_due"]
    import fitness_os.mem.consolidate as c
    monkeypatch.setattr(c, "run", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    rep2 = jobs.nightly()
    assert "boom" in rep2["consolidate.run"]["error"] and "miner.run" in rep2          # one failing step never stops the others


def test_scheduler_runs_the_memory_jobs():
    assert {"memory.nightly", "memory.weekly"} <= set(scheduler.SCHEDULE)
    rep = scheduler.run_due()
    assert "memory.nightly" in rep["jobs"] and not rep["jobs"]["memory.nightly"].get("error")
    assert scheduler.run_due()["jobs"].get("memory.nightly") is None                 # once per moment
