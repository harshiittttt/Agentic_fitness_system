"""migrate.py and the mirror in fitness_os/memory.py: legacy memories become beliefs, idempotently, and new writes stay in step. Offline."""
import pytest

from backend.memory import db
from fitness_os import memory
from fitness_os.mem import migrate, store


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("FITNESS_NOW", "2026-06-15T12:00:00+00:00")
    monkeypatch.setenv("FITNESS_EMBED", "hash")


def legacy_rows():
    """Rows written the way the app writes them today, but straight into the legacy tables (no mirroring yet)."""
    with memory._c() as c:
        for kind, body, sal, pin in [("semantic", "Pain/injury: left knee hurts on deep squats", 0.95, 1), ("semantic", "Dislikes: lunges", 0.7, 0),
                                     ("semantic", "Availability: 4 days per week", 0.8, 0), ("semantic", "PR on squat: 110 x 5 (2026-03-02)", 0.7, 0),
                                     ("procedural", "For this athlete, cutting volume 10% worked: readiness +9.", 0.7, 0),
                                     ("episodic", "Mar 2: Trained 3 sets (top: squat 100x5).", 0.5, 0), ("semantic", "I am vegetarian", 0.6, 0)]:
            c.execute("INSERT INTO memory_item (ts, last_seen, kind, body, salience, pinned, status) VALUES (?,?,?,?,?,?,'active')",
                      ("2026-03-02T09:00:00+00:00", "2026-03-02T09:00:00+00:00", kind, body, sal, pin))
        c.execute("INSERT INTO memory_item (ts, last_seen, kind, body, salience, pinned, status) VALUES ('2026-03-02T09:00:00+00:00','2026-03-02T09:00:00+00:00','semantic','You said you are vegan but logged chicken. Still true?',0.9,0,'flagged')")
        c.execute("INSERT INTO memory_item (ts, last_seen, kind, body, salience, pinned, status) VALUES ('2026-03-02T09:00:00+00:00','2026-03-02T09:00:00+00:00','semantic','Equipment: dumbbells only',0.7,0,'archived')")
    db.add_memory("injury", "right shoulder pinches on overhead press")
    db.add_memory("preference", "Prefers leg press over squats")
    db.add_memory("skip", "2026-03-02|lunges")


def test_legacy_rows_become_beliefs_with_sensible_predicates_sensitivity_and_pins():
    legacy_rows()
    rep = migrate.import_legacy()
    assert rep["memory_item"] == 8 and rep["memory"] == 3 and rep["skipped"] == 1 and "error" not in rep          # the flagged question is not a belief
    by_pred = {}
    for f in store.facts(status=None, limit=100):
        by_pred.setdefault(f["predicate"], []).append(f)
    assert all(f["source"] == "imported" for fs in by_pred.values() for f in fs)
    knee = next(f for f in by_pred["injury_site"] if "knee" in f["body"])
    assert knee["sensitivity"] == "health" and knee["pinned"] and knee["valid_from"].startswith("2026-03-02") and knee["evidence"][0]["t"] == "legacy"
    assert any("shoulder" in f["body"] for f in by_pred["injury_site"])
    assert by_pred["dislikes_exercise"][0]["object_text"] == "lunges" and by_pred["dislikes_exercise"][0]["polarity"] == -1
    assert by_pred["trains_days_per_week"][0]["object_text"] == "4" and by_pred["diet_type"][0]["object_text"] == "vegetarian"
    assert by_pred["lesson"][0]["kind"] == "procedural" and by_pred["happened"][0]["kind"] == "episodic" and by_pred["milestone"]
    assert any(f["object_text"] == "skipped lunges on 2026-06-15" for f in by_pred["happened"]) and by_pred["likes_exercise"][0]["object_text"] == "leg press"      # a legacy skip is the episode ingest writes
    archived = [f for f in store.facts(status=("archived",), limit=10)]
    assert len(archived) == 1 and archived[0]["predicate"] == "available_equipment"                              # statuses carry over


def test_the_import_is_idempotent():
    legacy_rows()
    first = migrate.import_legacy()
    n = store.stats()["facts_total"]
    second = migrate.import_legacy()
    assert second["memory_item"] == 0 and second["memory"] == 0 and second["already"] == first["memory_item"] + first["memory"] and store.stats()["facts_total"] == n


def test_the_same_injury_in_both_legacy_tables_is_one_belief():
    memory.add("semantic", "Pain/injury: left knee hurts on deep squats", 0.95, pinned=True, min_sim=0.98)
    db.add_memory("injury", "left knee hurts on deep squats")
    migrate.import_legacy()
    assert len([f for f in store.facts(predicate="injury_site") if "knee" in f["body"]]) == 1


def test_new_legacy_writes_are_mirrored_without_changing_what_memory_returns():
    a = memory.add("procedural", "Responds well to higher training frequency", 0.5)
    assert a == {"id": a["id"], "merged": False}
    fs = store.facts(predicate="lesson")
    assert len(fs) == 1 and fs[0]["source"] == "imported" and fs[0]["body"] == "Responds well to higher training frequency" and fs[0]["salience"] == 0.5
    before = fs[0]["alpha"]
    b = memory.add("procedural", "Responds well to higher training frequency!", 0.5)
    assert b == {"id": a["id"], "merged": True} and len(store.facts(predicate="lesson")) == 1 and store.facts(predicate="lesson")[0]["alpha"] > before   # a merge reinforces
    assert memory.get(a["id"])["hits"] == 2


def test_status_changes_are_mirrored_and_flagged_items_are_not_beliefs():
    a = memory.add("semantic", "Dislikes: burpees", 0.7)["id"]
    flagged = memory.add("semantic", "You said vegetarian but logged chicken. Still true?", 0.9, status="flagged")["id"]
    assert [f["predicate"] for f in store.facts()] == ["dislikes_exercise"]
    assert memory.set_status(a, "archived") and store.facts() == []
    assert memory.set_status(a, "active") and len(store.facts()) == 1
    assert memory.set_status(flagged, "resolved") and len(store.facts()) == 1
    assert memory.set_status(9999, "archived") is False


def test_existing_memory_is_imported_the_first_time_anything_is_written():
    legacy_rows()
    assert store.stats()["facts_total"] == 0
    memory.add("semantic", "Dislikes: burpees", 0.7)
    assert store.stats()["facts_total"] >= 11                                                                     # the lazy one-time import ran


def test_a_broken_belief_store_never_breaks_the_legacy_module(monkeypatch):
    monkeypatch.setattr(store, "add_fact", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = memory.add("semantic", "Dislikes: burpees", 0.7)
    assert r["merged"] is False and memory.get(r["id"])["body"] == "Dislikes: burpees" and memory.set_status(r["id"], "archived")
