"""Embeddings for the memory layer: real vectors when a model is reachable, a deterministic offline fallback otherwise. NEVER raises.

Real model : Gemini `gemini-embedding-001` through the OpenAI-compatible endpoint of backend.agents.base._cfg("main")
             (POST {base_url}/embeddings). We ask for 768 dimensions (`dimensions`), and if the server ignores or rejects that we truncate the
             native vector to 768 and re-normalise (the model is Matryoshka-trained, so a prefix is a valid smaller embedding).
Fallback   : "hash-512": signed feature hashing of word unigrams + bigrams, character 3-5-grams and a small fitness concept lexicon
             (abbreviations, Hinglish and Devanagari synonyms). Deterministic, offline, instant, no API key.

Vectors from different models are never compared: every vector is stored under its model name, and a call returns the vectors of ONE model
(`embed_texts_ex` says which). If any batch of a call fails, the whole call is answered by the fallback so a result is never a mixture.
Keys are read by backend.agents.base only; nothing here prints or logs them.
"""
from __future__ import annotations

import contextlib
import functools
import json
import os
import re
import time
import unicodedata
import zlib
from typing import Iterable, Iterator

import numpy as np

from . import store

HASH_DIM = 512
HASH_MODEL = "hash-512"
REAL_MODEL = "gemini-embedding-001"
REAL_DIM = 768
_BATCH = 48
_TIMEOUT = 12.0
_MAX_CHARS = 2000
_CACHE_MAX = 6000

_cache: dict[tuple[str, str], np.ndarray] = {}      # (model, text_hash) -> vector (process-wide; the DB holds the durable copy)
_state: dict = {"down_until": 0.0, "dims_ok": True, "offline": False, "last_error": "", "patient": False}
RATE_PER_MIN = int(os.environ.get("FITNESS_EMBED_RPM", "90"))      # the free Gemini tier allows 100 embedding inputs per minute
_sent: list[tuple[float, int]] = []                                  # (monotonic time, inputs) in the last minute


# ====================================================== text normalisation shared with retrieval ======================================================
_WORD = re.compile(r"[\wऀ-ॿ]+", re.UNICODE)
_STOP = set("""a an the and or of to in on at is are was were be been being it its this that these those for with from by as i me my mine you your yours we our they their them he she his her
do does did done doing have has had having will would can could should may might must shall about into over under than then so if but not no nor too very just also any all some more most such
what which who whom whose when where why how there here again once up down out off s t d ll m ve re
mera meri mere mujhe mujhko hum humara humare aap aapka aapki tum tumhara hai hain ho hota hoti hote tha thi the kya kab kaise kyun kaun kahan ka ki ke ko se mein me par pe aur bhi toh to yeh ye woh wo isse
है हैं हो होता होती होते था थी थे क्या कब कैसे क्यों कौन कहाँ का की के को से में पर और भी तो यह ये वो मेरा मेरी मेरे मुझे हम आप तुम""".split())

# Concept lexicon: each group is a set of surface forms (single tokens or two-word phrases) that mean the same thing for coaching purposes:
# abbreviations, plural/verb forms, Hinglish and Devanagari. It lets the offline embedder and BM25 query expansion bridge vocabulary gaps.
_GROUPS: dict[str, str] = {
    "knee": "knee knees kneecap patella ghutna ghutne ghutnon ghutno घुटना घुटने घुटनों",
    "shoulder": "shoulder shoulders delt delts deltoid rotator kandha kandhe कंधा कंधे कंधों",
    "back": "back lumbar spine spinal kamar कमर peeth पीठ",
    "hip": "hip hips kulha",
    "ankle": "ankle ankles takhna",
    "wrist": "wrist wrists kalai",
    "elbow": "elbow elbows kohni कोहनी",
    "neck": "neck gardan गर्दन",
    "pain": "pain painful hurt hurts hurting ache aches aching discomfort twinge pinch dard दर्द takleef तकलीफ",
    "injury": "injury injured injuries strain strained sprain sprained tear torn tendinitis tendonitis chot चोट",
    "sore": "sore soreness doms stiff stiffness",
    "sleep": "sleep slept sleeping asleep neend नींद insomnia bedtime nap naps",
    "tired": "tired fatigue fatigued exhausted drained lethargic thakan थकान thaka थका",
    "morning": "morning mornings subah सुबह dawn",
    "evening": "evening evenings shaam शाम",
    "night": "night nights raat रात nighttime",
    "weekend": "weekend weekends saturday sunday",
    "travel": "travel travelling traveling trip trips flight flights yatra safar",
    "work": "work working job office shift desk kaam काम naukri",
    "stress": "stress stressed anxious anxiety tension tanaav",
    "gym": "gym gyms",
    "home": "home ghar घर",
    "vegetarian": "vegetarian veg vegan plant-based shakahari शाकाहारी",
    "diet": "diet diets eat eats ate eating nutrition",
    "kilo": "kg kgs kilo kilos kilogram kilograms",
    "meat": "meat chicken mutton fish pork beef lamb",
    "egg": "egg eggs anda अंडा अंडे",
    "paneer": "paneer पनीर",
    "milk": "milk dudh दूध",
    "curd": "curd dahi yogurt yoghurt दही",
    "rice": "rice chawal चावल",
    "roti": "roti rotis chapati chapatti phulka रोटी",
    "dal": "dal daal lentil lentils दाल",
    "protein": "protein proteins whey",
    "calorie": "calorie calories kcal",
    "fatloss": "cut cutting slim shred fat-loss vajan वजन lose-weight lose-fat weight-loss fat-loss",
    "muscle": "bulk bulking hypertrophy mass gain-muscle build-muscle muscle-gain",
    "strength": "strength stronger strong powerlifting",
    "squat": "squat squats",
    "deadlift": "deadlift deadlifts dl rdl",
    "bench": "bench bp bench-press",
    "ohp": "ohp overhead-press shoulder-press military-press",
    "row": "row rows rowing",
    "pullup": "pullup pullups pull-up chinup chinups chin-up",
    "lunge": "lunge lunges",
    "legpress": "leg-press",
    "burpee": "burpee burpees",
    "cardio": "cardio run running jog jogging treadmill",
    "bodyweight": "bw bodyweight body-weight",
    "pr": "pr prs personal-record personal-best pb new-max",
    "rpe": "rpe effort exertion",
    "rir": "rir reps-in-reserve",
    "tdee": "tdee maintenance",
    "hate": "dislike dislikes hate hates detest despise loathe cant-stand",
    "love": "like likes love loves enjoy enjoys fond",
    "avoid": "avoid avoids skip skips skipped refuse refuses",
    "equipment": "dumbbell dumbbells barbell kettlebell kettlebells band bands cable cables rack",
    "allergy": "allergy allergic allergies intolerance intolerant",
    "short": "short quick brief",
    "doctor": "doctor physio physiotherapist physiotherapy therapist",
}
_CONCEPT_OF: dict[str, list[str]] = {}      # single token -> concept ids
_PHRASE_OF: dict[str, list[str]] = {}       # "stem stem" of a two-word form -> concept ids (hyphenated forms in the table are phrases)


def _build_lexicon() -> None:
    for cid, forms in _GROUPS.items():
        for form in forms.split():
            words = [w for w in form.lower().split("-") if w and w not in _STOP]
            if len(words) == 1:
                _CONCEPT_OF.setdefault(words[0], []).append(cid)
            elif len(words) >= 2:
                _PHRASE_OF.setdefault(f"{stem(words[0])} {stem(words[1])}", []).append(cid)


def stem(w: str) -> str:
    """Tiny, language-agnostic suffix stripper (enough to make plurals and gerunds collide; BM25 uses SQLite's porter on top)."""
    if len(w) <= 3 or not w.isascii():
        return w
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    for suf in ("ing", "ed"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            out = w[: -len(suf)]
            if len(out) >= 4 and out[-1] == out[-2] and out[-1] not in "lsz":       # squatting -> squat, planned -> plan
                out = out[:-1]
            return out
    if w.endswith("es") and len(w) > 4 and (w[-3] in "xz" or w[-4:-2] in ("ch", "sh", "ss")):
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


_build_lexicon()


def tokenize(text: str) -> list[str]:
    """Lowercased word tokens (Devanagari aware), stop words removed."""
    low = unicodedata.normalize("NFKC", str(text or "")).lower()
    return [w for w in _WORD.findall(low) if w not in _STOP and (len(w) > 1 or w.isdigit())]


def concepts_of(tokens: list[str]) -> list[str]:
    """Concept ids triggered by a token list (single tokens and adjacent-pair phrases)."""
    out: list[str] = []
    for i, t in enumerate(tokens):
        for key in (t, stem(t)):
            out += _CONCEPT_OF.get(key, [])
        if i + 1 < len(tokens):
            out += _PHRASE_OF.get(f"{stem(t)} {stem(tokens[i + 1])}", [])
    return list(dict.fromkeys(out))


def expand_query_terms(text: str) -> list[str]:
    """Extra surface forms for lexical search: the other members of every concept the query touches (so 'ghutna dard' also finds knee + pain)."""
    out: list[str] = []
    for c in concepts_of(tokenize(text)):
        forms = _GROUPS[c].split()
        out += [f for f in forms if "-" not in f][:6]
        out += [w for f in forms if "-" in f for w in f.split("-") if w not in _STOP][:4]
    return list(dict.fromkeys(out))


# ====================================================== the offline fallback ======================================================
def _h(s: str) -> int:
    return zlib.crc32(s.encode("utf-8"))


@functools.lru_cache(maxsize=16384)
def _hash_vec_cached(text: str) -> np.ndarray:
    toks = tokenize(text)
    feats: dict[str, float] = {}

    def put(f: str, w: float) -> None:
        feats[f] = feats.get(f, 0.0) + w

    stems = [stem(t) for t in toks]
    for s in stems:
        put("u:" + s, 1.0)
    for a, b in zip(stems, stems[1:]):
        put(f"b:{a}_{b}", 0.6)
    for t in toks:
        p = f"<{t}>"
        for n in (3, 4, 5):
            for i in range(0, max(0, len(p) - n + 1)):
                put("c:" + p[i:i + n], 0.25)
    for c in concepts_of(toks):
        put("k:" + c, 1.4)
    v = np.zeros(HASH_DIM, dtype="float32")
    for f, w in feats.items():
        hv = _h(f)
        v[hv % HASH_DIM] += (1.0 if (hv >> 20) & 1 else -1.0) * (1.0 + np.log(w) if w > 1.0 else w)
    n = float(np.linalg.norm(v))
    if n > 0:
        v /= n
    v.setflags(write=False)
    return v


def hash_embed(texts: Iterable[str]) -> np.ndarray:
    rows = [_hash_vec_cached(str(t or "")[:_MAX_CHARS]) for t in texts]
    return np.vstack(rows) if rows else np.zeros((0, HASH_DIM), dtype="float32")


# ====================================================== the real model ======================================================
try:                                    # import at module load: backend.agents.base reads the project .env when imported, which must not happen mid-request
    from backend.agents import base as _base
except Exception:                       # pragma: no cover
    _base = None


def _cfg() -> dict:
    try:
        return _base._cfg("main")
    except Exception:
        return {"base_url": "", "api_key": ""}


def real_model_name() -> str:
    return f"{os.environ.get('FITNESS_EMBED_MODEL') or REAL_MODEL}@{REAL_DIM}"


def set_offline(flag: bool) -> None:
    """Force the deterministic fallback (tests, benchmarks). `FITNESS_EMBED=hash` does the same from the environment."""
    _state["offline"] = bool(flag)


@contextlib.contextmanager
def patient() -> Iterator[None]:
    """Background mode (reindex, benchmarks): wait for rate-limit headroom instead of falling back to the offline model."""
    old = _state["patient"]
    _state["patient"] = True
    try:
        yield
    finally:
        _state["patient"] = old


def _acquire(n: int) -> bool:
    """Client-side rate limit. Foreground callers get False when there is no headroom (they use the fallback); patient callers wait (max ~3 min)."""
    deadline = time.monotonic() + 180
    while True:
        now = time.monotonic()
        _sent[:] = [(t, k) for t, k in _sent if now - t < 60]
        if sum(k for _, k in _sent) + n <= RATE_PER_MIN:
            _sent.append((now, n))
            return True
        if not _state["patient"] or now > deadline:
            return False
        time.sleep(min(4.0, max(0.5, 60 - (now - _sent[0][0]) + 0.2)) if _sent else 0.5)


def _real_available() -> bool:
    mode = os.environ.get("FITNESS_EMBED", "").lower()
    if _state["offline"] or mode in ("hash", "off", "offline"):
        return False
    if "PYTEST_CURRENT_TEST" in os.environ and mode != "real":     # tests are offline unless one opts in (a stray key loaded from .env must never reach the network)
        return False
    if time.monotonic() < _state["down_until"]:
        return False
    cfg = _cfg()
    return bool(cfg.get("api_key") and cfg.get("base_url"))


def model_name() -> str:
    """The model new vectors are made with right now (the real one while it is reachable, else the offline fallback)."""
    return real_model_name() if _real_available() else HASH_MODEL


def _unit(a: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(a, axis=1, keepdims=True)
    return (a / np.where(n == 0, 1.0, n)).astype("float32")


def _post(batch: list[str], cfg: dict) -> np.ndarray | None:
    import httpx
    body: dict = {"model": os.environ.get("FITNESS_EMBED_MODEL") or REAL_MODEL, "input": batch}
    tries = 4 if _state["patient"] else 2
    for attempt in range(tries):
        payload = dict(body)
        if _state["dims_ok"]:
            payload["dimensions"] = REAL_DIM
        try:
            r = httpx.post(f"{cfg['base_url']}/embeddings", headers={"Authorization": f"Bearer {cfg['api_key']}"}, json=payload, timeout=_TIMEOUT)
        except Exception as e:
            _state["last_error"] = f"{type(e).__name__}"
            if attempt < tries - 1:
                time.sleep(0.8)
                continue
            return None
        if r.status_code != 200:
            _state["last_error"] = f"HTTP {r.status_code}: {r.text[:120]}"
        if r.status_code == 400 and _state["dims_ok"]:
            _state["dims_ok"] = False                      # the server does not take `dimensions`: retry once without it, truncate ourselves
            continue
        if r.status_code in (401, 403):
            _state["down_until"] = time.monotonic() + 900   # bad/forbidden key: stop trying for a while
            return None
        if r.status_code == 429 and _state["patient"] and attempt < tries - 1:
            m = re.search(r"retry in ([0-9.]+)s", r.text)
            time.sleep(min(70.0, (float(m.group(1)) if m else 20.0) + 1.0))
            continue
        if r.status_code in (500, 502, 503, 504) and attempt < tries - 1:
            time.sleep(1.2)
            continue
        if r.status_code != 200:
            return None
        try:
            data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
            arr = np.asarray([d["embedding"] for d in data], dtype="float32")
        except Exception as e:
            _state["last_error"] = f"bad response: {type(e).__name__}"
            return None
        if arr.ndim != 2 or arr.shape[0] != len(batch):
            return None
        if arr.shape[1] > REAL_DIM:
            arr = arr[:, :REAL_DIM]
        return _unit(arr)
    return None


def _remote(texts: list[str]) -> np.ndarray | None:
    cfg = _cfg()
    model = real_model_name()
    out: list[np.ndarray | None] = [None] * len(texts)
    todo = []
    for i, t in enumerate(texts):
        v = _cache.get((model, store.text_hash(t)))
        if v is not None:
            out[i] = v
        else:
            todo.append(i)
    for s in range(0, len(todo), _BATCH):
        idx = todo[s:s + _BATCH]
        if not _acquire(len(idx)):
            return None                                    # no rate headroom right now: not an outage, so no breaker
        arr = _post([texts[i] or " " for i in idx], cfg)
        if arr is None:
            _state["down_until"] = max(_state["down_until"], time.monotonic() + (30 if "429" in _state["last_error"] else 120))
            return None
        for j, i in enumerate(idx):
            out[i] = arr[j]
            if len(_cache) > _CACHE_MAX:
                _cache.clear()
            _cache[(model, store.text_hash(texts[i]))] = arr[j]
    return np.vstack(out) if out else np.zeros((0, REAL_DIM), dtype="float32")


def embed_texts_ex(texts: list[str], task: str = "document") -> tuple[np.ndarray, str]:
    """-> (vectors [n, d] L2-normalised, the model that made ALL of them). Never raises."""
    texts = [str(t or "")[:_MAX_CHARS] for t in texts]
    if not texts:
        return np.zeros((0, HASH_DIM), dtype="float32"), HASH_MODEL
    try:
        if _real_available():
            arr = _remote(texts)
            if arr is not None:
                return arr, real_model_name()
    except Exception:
        pass
    try:
        return hash_embed(texts), HASH_MODEL
    except Exception:
        return np.zeros((len(texts), HASH_DIM), dtype="float32"), HASH_MODEL


def embed_texts(texts: list[str], task: str = "document") -> np.ndarray:
    """Vectors for `texts`, shape [n, d], L2-normalised. `task` is "document" or "query" (kept for the contract; the compatible endpoint has no task types)."""
    return embed_texts_ex(texts, task)[0]


def dense_floor(model: str) -> float:
    """Cosine below which a vector match is treated as noise (unrelated texts score near 0 under the hash fallback, far higher under a real model)."""
    env = os.environ.get("FITNESS_DENSE_FLOOR")
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    return 0.20 if model == HASH_MODEL else 0.58          # real model: unrelated pairs sit around 0.45-0.60 (benchmark), relevant ones 0.60-0.85


def dense_high(model: str) -> float:
    """Cosine treated as a 'full match' when scaling the dense signal to 0..1."""
    return 0.55 if model == HASH_MODEL else 0.80


# ====================================================== persistence ======================================================
def put_many(rows: list[tuple[str, int, np.ndarray, str]], model: str) -> None:
    """Store [(item_type, item_id, vector, text_hash)] under one model in a single transaction (the per-row store.set_embedding opens a
    connection each time, which is too slow for a bulk reindex). Same table, same format. Never raises."""
    if not rows:
        return
    try:
        with store._c() as c:
            c.executemany("INSERT OR REPLACE INTO mem_embedding (item_type, item_id, model, dim, vec, text_hash) VALUES (?,?,?,?,?,?)",
                          [(t, int(i), model, int(np.asarray(v).shape[0]), np.asarray(v, dtype="float32").tobytes(), h) for t, i, v, h in rows])
    except Exception:
        pass


def live_items() -> list[tuple[str, int, str]]:
    """Everything that should have a vector: active facts (body) and live discoveries (title + statement)."""
    out: list[tuple[str, int, str]] = []
    with store._c() as c:
        for r in c.execute("SELECT id, body FROM mem_fact WHERE status = 'active'"):
            out.append(("fact", r["id"], r["body"]))
        for r in c.execute("SELECT id, title, statement FROM mem_discovery WHERE status IN ('candidate','confirmed','weakening') AND user_state != 'denied'"):
            out.append(("discovery", r["id"], f"{r['title']}. {r['statement']}"))
    return out


_stored_cache: dict[tuple[str, str], tuple[tuple, dict]] = {}


def _stored(model: str) -> dict:
    """store.get_embeddings(model), memoised per database file and invalidated by a cheap fingerprint (row count, highest rowid). A stale vector can
    never be served: callers compare each vector's text_hash with the live text, and a mismatch is re-embedded."""
    from backend.memory import db
    try:
        with store._c() as c:
            n, mx = c.execute("SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM mem_embedding WHERE model = ?", (model,)).fetchone()
    except Exception:
        return store.get_embeddings(model)
    key = (db.DB_PATH, model)
    hit = _stored_cache.get(key)
    if hit and hit[0] == (n, mx):
        return hit[1]
    data = store.get_embeddings(model)
    _stored_cache[key] = ((n, mx), data)
    return data


def ensure_vectors(items: list[tuple[str, int, str]], model_hint: str | None = None, max_new: int | None = None) -> tuple[dict[tuple[str, int], np.ndarray], str]:
    """Vectors for `items` [(type, id, text)] under ONE model, embedding and persisting whatever is missing or stale. -> ({(type,id): vec}, model).
    `max_new` caps how many missing vectors are embedded in one call (items beyond it are simply absent from the result)."""
    model = model_hint or model_name()
    have = _stored(model)
    out: dict[tuple[str, int], np.ndarray] = {}
    todo: list[tuple[str, int, str, str]] = []
    for t, i, text in items:
        h = store.text_hash(text)
        g = have.get((t, i))
        if g is not None and g[1] == h:
            out[(t, i)] = g[0]
        else:
            todo.append((t, i, text, h))
    if max_new is not None:
        todo = todo[:max_new]
    if todo:
        arr, used = embed_texts_ex([x[2] for x in todo], "document")
        if used != model:                         # the real model failed mid-way: answer entirely from the fallback model instead (never mix)
            if model != HASH_MODEL:
                return ensure_vectors(items, HASH_MODEL, max_new)
        put_many([(t, i, arr[k], h) for k, (t, i, _, h) in enumerate(todo)], used)
        for k, (t, i, _, _) in enumerate(todo):
            out[(t, i)] = arr[k]
    return out, model


def reindex() -> dict:
    """Embed active facts and live discoveries whose text changed or that have no vector yet; drop vectors of items that are no longer live. Never raises."""
    try:
        with patient():
            return _reindex()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"[:160]}


def _reindex() -> dict:
    try:
        items = live_items()
        model = model_name()
        have = store.get_embeddings(model)
        live = {(t, i) for t, i, _ in items}
        stale = [(t, i) for (t, i) in have if (t, i) not in live]
        before = len(have)
        vecs, used = ensure_vectors(items, model)
        for t, i in stale:
            store.drop_embedding(t, i)
        embedded = len([1 for t, i, text in items if (t, i) in vecs]) - len([1 for t, i, text in items if (t, i) in have and have[(t, i)][1] == store.text_hash(text)])
        return {"model": used, "items": len(items), "embedded": max(0, embedded), "unchanged": len(items) - max(0, embedded), "dropped": len(stale), "had": before}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"[:160]}
