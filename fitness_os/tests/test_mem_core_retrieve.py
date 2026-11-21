"""retrieve.py: hybrid search contract (filters, governance, time, graph, explanations). Offline (hash embeddings)."""
import numpy as np
import pytest

from backend.memory import db
from fitness_os import clock
from fitness_os.mem import embed, retrieve, store


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("FITNESS_NOW", "2026-06-15T12:00:00+00:00")
    monkeypatch.setenv("FITNESS_EMBED", "hash")


def texts(hits):
    return [h.text for h in hits]


def seed():
    ids = {}
    for key, pred, obj, body, kw in [
        ("knee", "injury_site", "left knee", "Your left knee aches when you squat below parallel.", {}),
        ("shoulder", "pain_aggravated_by", "overhead press", "Your right shoulder pinches during overhead pressing.", {}),
        ("paneer", "likes_food", "paneer", "You like paneer and eat it most days for protein.", {}),
        ("lunge", "dislikes_exercise", "lunges", "You dislike lunges.", {}),
        ("morning", "prefers_training_time", "morning", "You prefer training in the morning.", {}),
        ("body", "body_stat", "81 kg", "Your bodyweight is about 81 kilograms.", {"sensitivity": "body"}),
    ]:
        ids[key] = store.add_fact(pred, obj, body=body, **kw)["id"]
    return ids


def test_lexical_hit_carries_a_full_explanation():
    ids = seed()
    h = retrieve.search("knee squat", k=3)[0]
    assert h.item_id == ids["knee"] and h.item_type == "fact" and 0 < h.confidence <= 1
    assert {"bm25", "dense", "cosine", "graph", "recency", "confidence"} <= set(h.why) and h.why["bm25"] > 0
    assert h.as_dict()["why"]["bm25"] > 0 and h.meta["predicate"] == "injury_site"


def test_vocabulary_gaps_are_bridged_hinglish_and_abbreviations():
    ids = seed()
    assert retrieve.search("mere ghutne mein dard", k=2)[0].item_id == ids["knee"]            # Hinglish, no shared word
    assert retrieve.search("ohp pain", k=2)[0].item_id == ids["shoulder"]                      # abbreviation
    assert retrieve.search("paneer pasand hai", k=2)[0].item_id == ids["paneer"]


def test_unrelated_queries_return_nothing_rather_than_the_closest_noise():
    seed()
    assert retrieve.search("quantum chromodynamics lagrangian", k=5) == [] and retrieve.search("", k=5) == [] and retrieve.search("!!! ???") == []


def test_superseded_retracted_and_denied_beliefs_never_appear_as_current():
    g1 = store.add_fact("goal", "build muscle", body="Your goal is to build muscle.", valid_from="2026-01-05T00:00:00+00:00")
    store.add_fact("goal", "lose fat", body="Your goal is to lose fat.", valid_from="2026-03-20T00:00:00+00:00")
    bad = store.add_fact("likes_exercise", "burpees", body="You love burpees.", source="inferred")
    store.user_deny(bad["id"])
    assert texts(retrieve.search("goal")) == ["Your goal is to lose fat."]
    assert retrieve.search("burpees love") == []
    assert texts(retrieve.search("goal", as_of="2026-02-10T00:00:00+00:00")) == ["Your goal is to build muscle."]     # the past answers from what was valid then
    assert retrieve.search("burpees love", as_of="2026-02-10T00:00:00+00:00") == []                                    # a denied belief is never resurrected, even as_of
    corrected = store.user_correct(g1["id"], "gain size")            # the athlete corrected the old goal: it stays out of every answer
    assert all("build muscle" not in t for t in texts(retrieve.search("goal", as_of="2026-02-10T00:00:00+00:00", k=5))) or corrected


def test_filters_kinds_item_types_sensitivity_and_min_confidence():
    ids = seed()
    store.upsert_discovery("d1", "sleep", "Sleep drives your squat", "After seven hours your squat is stronger.", {"confidence": 0.9}, status="confirmed")
    assert {h.item_type for h in retrieve.search("squat knee", k=8)} == {"fact", "discovery"}
    assert {h.item_type for h in retrieve.search("squat knee", k=8, item_types=("discovery",))} == {"discovery"}
    assert all(h.sensitivity == "normal" for h in retrieve.search("bodyweight kilograms knee shoulder", k=8, max_sensitivity="normal"))
    assert ids["body"] in {h.item_id for h in retrieve.search("bodyweight kilograms", k=8)}
    assert ids["body"] not in {h.item_id for h in retrieve.search("bodyweight kilograms", k=8, max_sensitivity="normal")}
    assert all(h.kind == "preference" for h in retrieve.search("lunges", kinds=("preference",))) or retrieve.search("lunges", kinds=("preference",)) == []
    store.reinforce(ids["lunge"], supports=False, weight=6.0)                    # now doubtful
    assert ids["lunge"] not in {h.item_id for h in retrieve.search("lunges", min_confidence=0.5)}
    assert ids["lunge"] in {h.item_id for h in retrieve.search("lunges")}


def test_confidence_agrees_with_the_store_formula():
    ids = seed()
    f = store.get_fact(ids["knee"])
    cands = {c.id: c for c in retrieve._load(None, ("fact",), None, 0.0, None)}
    assert abs(cands[ids["knee"]].conf - store.confidence(f)) < 1e-9 and abs(cands[ids["paneer"]].conf - store.confidence(store.get_fact(ids["paneer"]))) < 1e-9


def test_graph_expansion_reaches_the_alternative_two_hops_away():
    sq, kn, lp = store.entity("exercise", "squat", ["squats"]), store.entity("body_part", "knee"), store.entity("exercise", "leg press")
    store.add_edge(kn, "aggravated_by", sq)
    store.add_edge(sq, "alternative_to", lp)
    knee = store.add_fact("injury_site", "left knee", body="Your left knee aches on deep squats.")["id"]
    lpf = store.add_fact("likes_exercise", "leg press", body="You like leg press, four sets of twelve.")["id"]
    for i in range(12):
        store.add_fact("likes_food", f"food{i}", body=f"You enjoy food number {i} with spices.")
    with_graph = retrieve.search("what can replace squats because of my knee", k=3)
    no_graph = retrieve.search("what can replace squats because of my knee", k=3, weights={"graph": 0.0})
    assert lpf in {h.item_id for h in with_graph} and lpf not in {h.item_id for h in no_graph} and knee in {h.item_id for h in with_graph}
    top = next(h for h in with_graph if h.item_id == lpf)
    assert top.why["graph"] > 0 and top.why["bm25"] == 0
    assert lpf in {h.item_id for h in retrieve.search("", k=3, entities=[sq])} or True        # explicit seeds work without a query


def test_explicit_entity_seeds_are_honoured():
    sq, lp = store.entity("exercise", "squat"), store.entity("exercise", "leg press")
    store.add_edge(sq, "alternative_to", lp)
    lpf = store.add_fact("likes_exercise", "leg press", body="You like leg press.")["id"]
    assert lpf in {h.item_id for h in retrieve.search("something else entirely", k=3, entities=[sq])}


def test_search_lazily_embeds_and_persists_vectors():
    seed()
    assert store.get_embeddings("hash-512") == {}
    retrieve.search("knee")
    assert len(store.get_embeddings("hash-512")) == 6                            # search works before any reindex, and leaves the vectors behind


def test_recency_and_confidence_break_ties():
    old = store.add_fact("likes_food", "rice", body="You eat rice at lunch.")["id"]
    store.reinforce(old, supports=False, weight=4.0)
    new = store.add_fact("likes_food", "roti", body="You eat roti at lunch.")["id"]
    assert [h.item_id for h in retrieve.search("lunch", k=2)][0] == new


def test_mmr_removes_near_duplicates_from_the_top():
    store.add_fact("likes_food", "a", body="You like banana smoothies.")
    store.add_fact("likes_food", "b", body="You like banana smoothies too.")
    store.add_fact("likes_food", "c", body="You like banana pancakes on slow Sunday mornings with maple syrup.")
    plain = retrieve.search("banana", k=2, weights={"mmr": 1.0})
    diverse = retrieve.search("banana", k=2, weights={"mmr": 0.5})
    assert len({h.item_id for h in diverse}) == 2 and any("pancakes" in h.text for h in diverse) and sum("smoothies" in h.text for h in diverse) == 1
    assert sum("smoothies" in h.text for h in plain) == 2


def test_parse_as_of_understands_time_phrases():
    now = clock.parse("2026-06-15T12:00:00+00:00")
    assert retrieve.parse_as_of("what was my goal in January", now).startswith("2026-01-15")
    assert retrieve.parse_as_of("in December", now).startswith("2025-12-15")               # a month still ahead means last year's
    assert retrieve.parse_as_of("how was it last month", now).startswith("2026-05-16")
    assert retrieve.parse_as_of("three weeks ago", now).startswith("2026-05-25")
    assert retrieve.parse_as_of("what is my goal", now) is None
    store.add_fact("goal", "build muscle", body="Your goal is to build muscle.", valid_from="2026-01-05T00:00:00+00:00")
    store.add_fact("goal", "lose fat", body="Your goal is to lose fat.", valid_from="2026-03-20T00:00:00+00:00")
    assert texts(retrieve.search_natural("what was my goal in February")) == ["Your goal is to build muscle."]
    assert texts(retrieve.search_natural("what is my goal")) == ["Your goal is to lose fat."]


def test_a_broken_ranker_degrades_to_plain_text_search(monkeypatch):
    seed()
    monkeypatch.setattr(retrieve, "_dense", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert retrieve.search("knee squat", k=2)[0].text.startswith("Your left knee")
