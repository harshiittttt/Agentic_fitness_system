"""consolidate.py: the idempotent sleep pass (promote, merge, resolve, decay, link, re-embed). Offline."""
import importlib
import json

import pytest

from backend.memory import db
from fitness_os.mem import embed, store

consolidate = importlib.import_module("fitness_os.mem.consolidate")      # `mem.consolidate` is also the facade function


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("FITNESS_NOW", "2026-06-15T12:00:00+00:00")
    monkeypatch.setenv("FITNESS_EMBED", "hash")


def snapshot():
    with store._c() as c:
        return [tuple(r) for r in c.execute("SELECT id, status, alpha, beta, user_state, valid_to, evidence, object_entity FROM mem_fact ORDER BY id")], \
               [tuple(r) for r in c.execute("SELECT src, rel, dst, weight FROM mem_edge ORDER BY id")]


def episodes(n, text="skipped breakfast then trained legs hard", pred="happened", start=1):
    return [store.add_fact(pred, f"{text} day {i}", body=f"Jun {i}: {text}", kind="episodic", valid_from=f"2026-06-{i:02d}T08:00:00+00:00")["id"] for i in range(start, start + n)]


def test_repeated_episodes_are_promoted_once_with_evidence_and_then_only_reinforced():
    ids = episodes(4)
    rep = consolidate.run()
    assert rep["promote"]["promoted"] == 1
    sem = [f for f in store.facts(predicate="recurring")]
    assert len(sem) == 1 and sem[0]["kind"] == "semantic" and sem[0]["source"] == "inferred" and "4 times" in sem[0]["body"]
    assert {e["id"] for e in sem[0]["evidence"]} == set(ids) and sem[0]["meta"]["n_sources"] == 4
    assert consolidate.run()["promote"] == {"promoted": 0, "reinforced": 0}                       # idempotent
    episodes(2, start=5)
    again = consolidate.run()["promote"]
    assert again["promoted"] == 0 and again["reinforced"] == 1 and len(store.facts(predicate="recurring")) == 1
    assert store.facts(predicate="recurring")[0]["meta"]["n_sources"] == 6


def test_too_few_or_unrelated_episodes_are_not_promoted():
    episodes(2)
    store.add_fact("happened", "a trip", body="Jun 9: flew to Mumbai for a wedding", kind="episodic", valid_from="2026-06-09T08:00:00+00:00")
    store.add_fact("happened", "a flu", body="Jun 12: caught the flu and stayed in bed", kind="episodic", valid_from="2026-06-12T08:00:00+00:00")
    assert consolidate.run()["promote"]["promoted"] == 0 and store.facts(predicate="recurring") == []


def test_what_the_athlete_denied_is_never_regenerated():
    episodes(4)
    consolidate.run()
    store.user_deny(store.facts(predicate="recurring")[0]["id"], "no that is not me")
    episodes(2, start=5)
    consolidate.run()
    assert store.facts(predicate="recurring") == []


def test_near_duplicates_merge_keeping_the_stronger_and_the_evidence():
    a = store.add_fact("injury_site", "left knee hurts when squatting deep", body="Left knee hurts when squatting deep.", source="inferred", evidence=[{"t": "set", "id": 1}])["id"]
    b = store.add_fact("injury_site", "your left knee hurts when you squat deep", body="Your left knee hurts when you squat deep.", source="stated", evidence=[{"t": "chat", "id": 2}])["id"]
    rep = consolidate.run()
    assert rep["merge"]["merged"] == 1
    fa, fb = store.get_fact(a), store.get_fact(b)
    assert fb["status"] == "active" and fa["status"] == "superseded" and fa["meta"]["merged_into"] == b           # the stated one wins
    assert {"t": "set", "id": 1} in fb["evidence"] and {"t": "chat", "id": 2} in fb["evidence"] and fb["alpha"] > 3.0
    assert consolidate.run()["merge"]["merged"] == 0 and ("fact", a) not in store.get_embeddings("hash-512") and ("fact", b) in store.get_embeddings("hash-512")


def test_things_that_differ_in_number_polarity_or_meaning_are_not_merged():
    store.add_fact("likes_exercise", "bench press 3 sets", body="You do bench press for 3 sets.")
    store.add_fact("likes_exercise", "bench press 5 sets", body="You do bench press for 5 sets.")
    store.add_fact("likes_exercise", "bench press", body="You like bench press.")
    store.add_fact("likes_exercise", "bench dips", body="You like bench dips.")
    store.add_fact("dislikes_exercise", "bench press", body="You dislike bench press.")
    assert consolidate.run()["merge"]["merged"] == 0 and len(store.facts(limit=50)) == 5


def test_confirmed_beliefs_are_never_merged_away_only_reinforced():
    a = store.add_fact("injury_site", "left knee hurts when squatting deep", body="Left knee hurts when squatting deep.", source="stated")["id"]
    b = store.add_fact("injury_site", "your left knee hurts when you squat deep", body="Your left knee hurts when you squat deep.", source="inferred")["id"]
    store.user_confirm(a)
    consolidate.run()
    assert store.get_fact(a)["status"] == "active" and store.get_fact(a)["user_state"] == "confirmed" and store.get_fact(b)["status"] == "superseded"
    c = store.add_fact("likes_food", "oats with banana in the morning", body="You like oats with banana in the morning.")["id"]
    d = store.add_fact("likes_food", "morning oats with banana", body="You like morning oats with banana.")["id"]
    store.user_confirm(c), store.user_confirm(d)
    consolidate.run()
    assert store.get_fact(c)["status"] == "active" and store.get_fact(d)["status"] == "active"            # both vouched for by the athlete: untouched


def test_a_dispute_never_overrides_the_confirmed_value_and_a_stale_one_is_archived(monkeypatch):
    a = store.add_fact("trains_days_per_week", "4", body="You train four days a week.", source="stated")["id"]
    store.user_confirm(a)
    d = store.add_fact("trains_days_per_week", "2", body="Your logs show two days a week.", source="observed")
    assert d["disputed"] == [a]
    rep = consolidate.run()
    assert rep["resolve"]["disputes_kept_confirmed"] == 1 and store.get_fact(a)["status"] == "active" and store.get_fact(d["id"])["status"] == "disputed"
    monkeypatch.setenv("FITNESS_NOW", "2026-09-15T12:00:00+00:00")                                      # three months of silence
    rep = consolidate.run()
    assert store.get_fact(d["id"])["status"] == "archived" and store.get_fact(a)["status"] == "active"


def test_a_persistent_strong_dispute_becomes_a_question_for_the_athlete_once():
    a = store.add_fact("trains_days_per_week", "4", body="You train four days a week.", source="stated")["id"]
    store.user_confirm(a)
    d = store.add_fact("trains_days_per_week", "2", body="Your logs show two days a week.", source="observed")["id"]
    for _ in range(4):
        store.reinforce(d, True, weight=2.0)
    consolidate.run(), consolidate.run()
    qs = store.questions()
    assert len(qs) == 1 and qs[0]["topic"] == "dispute:trains_days_per_week" and "Which is right" in qs[0]["question"]
    assert store.get_fact(a)["status"] == "active"


def test_a_dispute_whose_rival_disappeared_stands_on_its_own():
    a = store.add_fact("trains_days_per_week", "4", source="stated")["id"]
    store.user_confirm(a)
    d = store.add_fact("trains_days_per_week", "2", source="observed")["id"]
    store.user_deny(a)
    consolidate.run()
    assert store.get_fact(d)["status"] == "active"


def test_decay_archives_the_discredited_and_the_forgotten_but_never_the_pinned_or_confirmed(monkeypatch):
    weak = store.add_fact("likes_food", "kale", body="You like kale.", source="inferred")["id"]
    store.reinforce(weak, supports=False, weight=8.0)
    pinned = store.add_fact("injury_site", "left knee", body="Your left knee hurts.")["id"]
    conf = store.add_fact("likes_food", "oats", body="You like oats.")["id"]
    store.user_confirm(conf)
    old = store.add_fact("travel_pattern", "trips", body="You travel monthly.")["id"]
    assert consolidate.run()["decay"]["archived"] == 1 and store.get_fact(weak)["status"] == "archived"
    monkeypatch.setenv("FITNESS_NOW", "2027-09-15T12:00:00+00:00")                                      # 15 months on: the unreinforced travel note is past four half-lives
    consolidate.run()
    assert store.get_fact(old)["status"] == "archived" and store.get_fact(pinned)["status"] == "active" and store.get_fact(conf)["status"] == "active"


def test_entities_and_edges_are_refreshed_from_fact_text_without_inflating_weights():
    store.add_fact("injury_site", "left knee", body="Your left knee aches on deep squats.")
    store.add_fact("likes_exercise", "leg press", body="You like leg press, it is gentle on the knee.")
    rep = consolidate.run()
    names = {e["name"] for e in [store.get_entity(i) for i in range(2, 8)] if e}
    assert {"knee", "squat", "leg press"} <= names and rep["link"]["edges"] >= 2 and rep["link"]["linked_objects"] >= 1
    kn = store.entity("body_part", "knee")
    others = {store.get_entity(n["other"])["name"] for n in store.neighbors(kn)}
    assert {"squat", "leg press"} <= others
    before = snapshot()
    consolidate.run()
    assert snapshot() == before                                                                          # a second night changes nothing


def test_the_whole_pass_is_idempotent_and_the_report_is_complete():
    episodes(4)
    store.add_fact("injury_site", "left knee hurts when squatting deep", body="Left knee hurts when squatting deep.", source="inferred")
    store.add_fact("injury_site", "your left knee hurts when you squat deep", body="Your left knee hurts when you squat deep.", source="stated")
    store.add_fact("likes_food", "kale", body="You like kale.", source="inferred")
    first = consolidate.run()
    assert {"promote", "merge", "resolve", "decay", "link", "reindex", "baseline"} <= set(first) and first["reindex"]["model"] == "hash-512"
    snap = snapshot()
    second = consolidate.run()
    assert snapshot() == snap and second["promote"] == {"promoted": 0, "reinforced": 0} and second["merge"]["merged"] == 0 and second["reindex"]["embedded"] == 0


def test_a_failing_step_is_reported_and_the_rest_still_run(monkeypatch):
    store.add_fact("likes_food", "oats", body="You like oats.")
    monkeypatch.setattr(consolidate, "_merge", lambda rows: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(embed, "reindex", lambda: (_ for _ in ()).throw(RuntimeError("bang")))
    rep = consolidate.run()
    assert "boom" in rep["merge"]["error"] and "decay" in rep and "link" in rep and "bang" in rep["reindex"]["error"]


def test_the_module_stays_callable_as_the_facade_even_after_it_is_imported():
    from fitness_os import mem
    store.add_fact("likes_food", "oats", body="You like oats.")
    assert "promote" in mem.consolidate() and "promote" in mem.consolidate()        # twice: the first call imports the module over the facade function
