"""The labelled retrieval benchmark as an acceptance test (offline: the deterministic hash embeddings; the real-model numbers are produced by hand
with `python -m fitness_os.mem.bench_retrieval --real`). One run of the benchmark is shared by all the assertions."""
import pytest

from fitness_os.mem import bench_retrieval as B
from fitness_os.mem import embed


@pytest.fixture(scope="module")
def result():
    old = dict(embed._state)
    try:
        yield B.evaluate(real=False, do_tune=False, verbose=False)
    finally:
        embed._state.update(old)


def test_the_benchmark_is_big_and_varied_enough(result):
    cats = {q[0] for q in B.QUERIES}
    assert result["n_beliefs"] >= 150 and len(B.QUERIES) >= 60 and result["model"] == "hash-512"
    assert {"paraphrase", "abbrev", "hinglish", "negation", "temporal", "asof", "entity", "multihop", "distractor"} <= cats
    assert any(q[2] == [] for q in B.QUERIES)                        # queries whose only candidates are denied beliefs


def test_hybrid_beats_bm25_only_and_dense_only_on_recall_at_5_and_mrr(result):
    for split in ("all", "test"):
        r = result[split]
        for single in ("BM25 only", "dense only"):
            assert r["hybrid"]["r5"] >= r[single]["r5"] and r["hybrid"]["mrr"] >= r[single]["mrr"], (split, single, r["hybrid"], r[single])
    r = result["all"]
    assert r["hybrid"]["r5"] > max(r["BM25 only"]["r5"], r["dense only"]["r5"]) and r["hybrid"]["mrr"] > max(r["BM25 only"]["mrr"], r["dense only"]["mrr"])


def test_superseded_and_denied_beliefs_never_come_back_as_current(result):
    for name, r in result["all"].items():
        assert r["violations"] == 0, name


def test_the_offline_fallback_still_clears_a_lower_bar(result):
    h = result["all"]["hybrid"]
    assert h["r5"] >= 0.62 and h["mrr"] >= 0.60 and h["ndcg5"] >= 0.55
    cats = h["by_cat"]
    assert cats["abbrev"]["r5"] >= 0.85 and cats["hinglish"]["r5"] >= 0.75 and cats["distractor"]["r5"] >= 0.8 and cats["entity"]["r5"] >= 0.9
    assert cats["multihop"]["r5"] >= 0.4 and cats["temporal"]["r5"] >= 0.6 and cats["asof"]["r5"] >= 0.6          # the graph and valid-time paths work


def test_metrics_are_computed_correctly():
    m = B._metrics(["x", "a", "y", "b"], ["a", "b"])
    assert m["r1"] == 0 and m["r5"] == 1 and abs(m["mrr"] - 0.5) < 1e-9 and 0 < m["ndcg5"] < 1 and m["hit5"] == 1
    assert B._metrics(["a"], ["a"])["ndcg5"] == 1.0 and B._metrics(["z"], ["a"])["mrr"] == 0
    train, test = B.split(B.QUERIES)
    assert len(train) + len(test) == len(B.QUERIES) and 0.28 < len(test) / len(B.QUERIES) < 0.38
