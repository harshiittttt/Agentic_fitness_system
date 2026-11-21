"""embed.py: deterministic offline vectors, the real-model path (faked), caching, rate limit, never mixing models, reindex. Fully offline."""
import numpy as np
import pytest

from backend.memory import db
from fitness_os.mem import embed, store


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("FITNESS_NOW", "2026-03-10T12:00:00+00:00")
    monkeypatch.setenv("FITNESS_EMBED", "hash")
    monkeypatch.setattr(embed, "_cache", {})
    monkeypatch.setattr(embed, "_sent", [])
    monkeypatch.setitem(embed._state, "down_until", 0.0)
    monkeypatch.setitem(embed._state, "dims_ok", True)


def cos(a, b):
    return float(a @ b)


def test_hash_vectors_are_deterministic_normalised_and_model_named():
    a = embed.embed_texts(["Your left knee aches when you squat."])
    b = embed.embed_texts(["Your left knee aches when you squat."])
    assert a.shape == (1, 512) and np.allclose(a, b) and abs(float(np.linalg.norm(a[0])) - 1.0) < 1e-5
    assert embed.model_name() == "hash-512"


def test_related_texts_are_closer_than_unrelated_including_hinglish_and_devanagari():
    v = embed.embed_texts(["Your left knee aches when you squat below parallel.", "mere ghutne mein dard hota hai", "knee pain on deep squats",
                           "You enjoy paneer for protein.", "आपको पनीर पसंद है", "paneer pasand hai"])
    assert cos(v[0], v[2]) > 0.3 and cos(v[0], v[1]) > cos(v[0], v[3]) + 0.1          # English paraphrase and Hinglish both beat an unrelated belief
    assert cos(v[3], v[5]) > cos(v[3], v[0]) and cos(v[4], v[5]) > 0.2                 # Devanagari and Roman Hindi meet through the concept lexicon


def test_it_never_raises_on_odd_input():
    out = embed.embed_texts(["", "   ", None, "x" * 10000, "\x00\x01 ??? ​", "🙂"])  # type: ignore[list-item]
    assert out.shape[0] == 6 and np.isfinite(out).all()
    assert embed.embed_texts([]).shape[0] == 0


def test_lexicon_bridges_abbreviations_and_languages():
    assert "ohp" in embed.concepts_of(embed.tokenize("overhead press")) and "pr" in embed.concepts_of(embed.tokenize("personal best"))
    assert {"knee", "pain"} <= set(embed.concepts_of(embed.tokenize("ghutne dard")))
    assert "squat" in embed.expand_query_terms("squats") or "squats" in embed.expand_query_terms("squat")


# ------------------------------------------------------------------ the real model path, faked
class FakeNet:
    def __init__(self, dim=3072, fail=False):
        self.calls, self.dim, self.fail = [], dim, fail

    def __call__(self, batch, cfg):
        self.calls.append(list(batch))
        if self.fail:
            return None
        rng = [np.random.default_rng(__import__("zlib").crc32(t.encode())).normal(size=self.dim) for t in batch]
        arr = np.asarray(rng, dtype="float32")[:, : embed.REAL_DIM]
        return embed._unit(arr)


@pytest.fixture
def real(monkeypatch):
    monkeypatch.setenv("FITNESS_EMBED", "real")
    monkeypatch.setattr(embed, "_cfg", lambda: {"base_url": "http://x", "api_key": "k"})
    net = FakeNet()
    monkeypatch.setattr(embed, "_post", net)
    return net


def test_real_model_is_used_when_available_and_cached_by_text(real):
    assert embed.model_name() == "gemini-embedding-001@768"
    v, m = embed.embed_texts_ex(["alpha", "beta"], "document")
    assert m == "gemini-embedding-001@768" and v.shape == (2, 768) and abs(float(np.linalg.norm(v[0])) - 1) < 1e-5
    embed.embed_texts_ex(["alpha", "beta", "gamma"])
    assert real.calls == [["alpha", "beta"], ["gamma"]]                        # nothing is embedded twice


def test_failure_falls_back_for_the_whole_call_never_a_mixture(real, monkeypatch):
    embed.embed_texts_ex(["cached text"])
    monkeypatch.setattr(embed, "_post", FakeNet(fail=True))
    v, m = embed.embed_texts_ex(["cached text", "new text"])
    assert m == "hash-512" and v.shape == (2, 512)                             # one model for the whole result
    assert embed.model_name() == "hash-512"                                    # and the breaker keeps us offline for a while, instead of timing out per call


def test_ensure_vectors_never_mixes_models(real, monkeypatch):
    items = [("fact", 1, "one"), ("fact", 2, "two")]
    store.add_fact("likes_food", "one"), store.add_fact("likes_food", "two")
    vecs, model = embed.ensure_vectors(items)
    assert model == "gemini-embedding-001@768" and len(vecs) == 2
    monkeypatch.setattr(embed, "_post", FakeNet(fail=True))
    more, model2 = embed.ensure_vectors(items + [("fact", 3, "three")])
    assert model2 == "hash-512" and {v.shape for v in more.values()} == {(512,)}
    assert set(store.get_embeddings("gemini-embedding-001@768")) == {("fact", 1), ("fact", 2)}
    assert ("fact", 3) in store.get_embeddings("hash-512")


def test_dimensions_param_rejected_then_truncated(monkeypatch):
    """A server that rejects `dimensions` is retried without it and the native vector is truncated to 768 and re-normalised."""
    import httpx
    monkeypatch.setenv("FITNESS_EMBED", "real")
    monkeypatch.setattr(embed, "_cfg", lambda: {"base_url": "http://x", "api_key": "k"})
    seen = []

    class R:
        def __init__(self, code, body):
            self.status_code, self._b, self.text = code, body, str(body)

        def json(self):
            return self._b

    def post(url, headers=None, json=None, timeout=None):
        seen.append("dimensions" in json)
        if "dimensions" in json:
            return R(400, {"error": "bad"})
        return R(200, {"data": [{"index": i, "embedding": list(np.random.default_rng(i).normal(size=3072))} for i in range(len(json["input"]))]})
    monkeypatch.setattr(httpx, "post", post)
    v, m = embed.embed_texts_ex(["a", "b"])
    assert seen == [True, False] and m.startswith("gemini") and v.shape == (2, 768) and abs(float(np.linalg.norm(v[1])) - 1) < 1e-5


def test_foreground_calls_use_the_fallback_when_over_the_rate_limit(real, monkeypatch):
    monkeypatch.setattr(embed, "RATE_PER_MIN", 3)
    assert embed.embed_texts_ex(["a", "b"])[1].startswith("gemini")
    assert embed.embed_texts_ex(["c", "d"])[1] == "hash-512"                    # only 1 unit of headroom left: answer offline, do not stall the app
    assert embed._acquire(1) and not embed._acquire(5)


# ------------------------------------------------------------------ reindex
def test_reindex_embeds_changed_text_only_and_drops_dead_items():
    a = store.add_fact("likes_food", "oats", body="You eat oats for breakfast.")["id"]
    b = store.add_fact("likes_food", "paneer", body="You like paneer.")["id"]
    d = store.upsert_discovery("k", "fam", "Sleep matters", "You lift more after sleep.", {"confidence": 0.9}, status="confirmed")["id"]
    r1 = embed.reindex()
    assert r1["model"] == "hash-512" and r1["embedded"] == 3 and {k for k in store.get_embeddings("hash-512")} == {("fact", a), ("fact", b), ("discovery", d)}
    r2 = embed.reindex()
    assert r2["embedded"] == 0 and r2["unchanged"] == 3                         # idempotent
    store.update_fact(a, body="You eat oats with banana for breakfast.")
    store.user_deny(b)
    r3 = embed.reindex()
    assert r3["embedded"] == 1 and set(store.get_embeddings("hash-512")) == {("fact", a), ("discovery", d)}
    vec, h = store.get_embeddings("hash-512")[("fact", a)]
    assert h == store.text_hash("You eat oats with banana for breakfast.")


def test_reindex_never_raises(monkeypatch):
    monkeypatch.setattr(embed, "live_items", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert "error" in embed.reindex()
