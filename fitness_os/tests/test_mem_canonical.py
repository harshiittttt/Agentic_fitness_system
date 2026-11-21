"""One canonical predicate per concept: aliases and legacy shapes land on the same belief, and the consolidation pass merges old duplicates. Offline."""
import pytest

from backend.memory import db
from fitness_os.mem import consolidate, ingest, store


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("FITNESS_NOW", "2026-06-15T12:00:00+00:00")
    monkeypatch.setenv("FITNESS_EMBED", "hash")


def live(pred=None):
    return [f for f in store.facts(status=("active",), limit=100) if pred is None or f["predicate"] == pred]


def test_canonical_is_idempotent_and_maps_aliases():
    assert store.canonical_pred("Injuries") == "injury_site" and store.canonical_pred("likes_exercise") == "likes_exercise"
    p, o, m = store.canonical("skips_exercise", "Lunges", "2026-03-02T09:00:00+00:00")
    assert (p, o) == ("happened", "skipped lunges on 2026-03-02") and m["date"] == "2026-03-02"
    assert store.canonical(p, o, None, m)[:2] == (p, o)
    assert store.canonical("injury_site", "Pain or injury: left knee.")[1] == "left knee" and store.canonical("injury_site", "left knee")[1] == "left knee"


def test_an_alias_and_the_registered_predicate_are_one_belief():
    a = store.add_fact("injury_site", "left knee", source="stated")
    b = store.add_fact("injuries", "left knee", source="stated")
    assert b["id"] == a["id"] and b["reinforced"] and len(live("injury_site")) == 1
    store.add_fact("dislikes_exercises", "lunges", source="stated")
    assert len(live("dislikes_exercise")) == 1


def test_a_legacy_skip_and_an_ingest_skip_are_one_episode():
    store.add_fact("skips_exercise", "lunges", body="You skipped lunges.", kind="episodic", source="imported", valid_from="2026-06-15T09:00:00+00:00")
    ingest._skip_event({"exercise": "lunges", "date": "2026-06-15"})
    rows = [f for f in live("happened") if "skipped lunges" in f["object_text"]]
    assert len(rows) == 1 and not live("skips_exercise")


def test_consolidation_merges_existing_duplicates_and_is_idempotent():
    store.add_fact("injury_site", "left knee", source="stated")
    store.add_fact("likes_food", "seed")                                     # make sure the schema exists
    now = store.now()
    with store._c() as c:                                                    # rows as an older build stored them: raw alias / legacy shapes
        for pred, obj in [("injuries", "left knee"), ("injury_site", "Pain or injury: right shoulder"), ("skips_exercise", "rows"), ("preferred_training_time", "morning")]:
            c.execute("INSERT INTO mem_fact (subject_id, predicate, object_text, object_key, kind, body, source, sensitivity, alpha, beta, confidence, salience, pinned, status,"
                      " user_state, valid_from, observed_at, last_reinforced) VALUES (1,?,?,?,'semantic',?,'imported','normal',2.5,1.0,0.7,0.5,0,'active','none',?,?,?)",
                      (pred, obj, store.norm(obj), obj, "2026-03-02T09:00:00+00:00", now, now))
    rep = consolidate.canonicalize()
    assert rep["merged"] == 1 and rep["renamed"] == 3
    assert len(live("injury_site")) == 2 and {f["object_text"] for f in live("injury_site")} == {"left knee", "right shoulder"}
    assert live("happened")[0]["object_text"] == "skipped rows on 2026-03-02" and live("prefers_training_time")[0]["object_text"] == "morning"
    again = consolidate.canonicalize()
    assert again == {"merged": 0, "renamed": 0, "skipped": 0}
    assert consolidate.run()["canon"] == again


def test_a_denial_survives_canonicalisation():
    f = store.add_fact("injury_site", "left knee", source="stated")["id"]
    store.user_deny(f) if hasattr(store, "user_deny") else store.update_fact(f, user_state="denied", status="retracted")
    assert store.add_fact("injuries", "left knee", source="inferred")["blocked"]
