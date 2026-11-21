"""Meal text parsing: offline parser fixes + the hybrid LLM path (always mocked, no network)."""
import time

import pytest

from backend.memory import db
from backend.tools import meal_log
from backend.tools.foods import FOODS, macros, parse_meal_text


def by(text):
    r = parse_meal_text(text)
    return {i["name"]: i for i in r["items"]}, r


@pytest.fixture
def llm(monkeypatch, tmp_path):
    """Pretend a key exists; record LLM calls; the reply is set via llm.reply."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "m.db"))
    db.init() if hasattr(db, "init") else None

    class L:
        calls: list = []
        reply: object = {"items": []}

    def fake(system, user, tool, max_tokens=1024):
        L.calls.append(user)
        if isinstance(L.reply, Exception):
            raise L.reply
        if callable(L.reply):
            return L.reply()
        return L.reply

    L.calls = []
    monkeypatch.setattr(meal_log, "have_key", lambda role="main": True)
    monkeypatch.setattr(meal_log, "extract_structured", fake)
    return L


# ---------------- offline parser ----------------
def test_grams_and_count():
    r, _ = by("400 grams rice")
    assert r["white rice"]["grams"] == 400 and r["white rice"]["kcal"] == 520
    r, _ = by("4 eggs")
    assert r["egg"]["grams"] == 200 and r["egg"]["kcal"] == 286


def test_decimals_are_not_split():
    r, _ = by("1.5 cups dal")
    assert r["dal"]["grams"] == 225  # 1.5 cups at 150 g, not 1000 g
    r, _ = by("0.5 kg paneer")
    assert r["paneer"]["grams"] == 500


@pytest.mark.parametrize("text,food,grams", [
    ("half kg paneer", "paneer", 500), ("1/2 kg paneer", "paneer", 500), ("half litre milk", "milk", 500),
    ("two hundred grams chicken", "chicken breast", 200), ("one and a half cup rice", "white rice", 225),
    ("ek glass doodh", "milk", 250), ("dedh glass lassi", "lassi", 375), ("rice 200 g", "white rice", 200),
    ("half dozen eggs", "egg", 300), ("a dozen eggs", "egg", 600),
])
def test_quantity_forms(text, food, grams):
    r, res = by(text)
    assert r[food]["grams"] == grams and not res["unknown"], res


def test_full_meal_sentence():
    r, res = by("had half kg paneer, 3 rotis with dal and a glass of lassi")
    assert r["paneer"]["grams"] == 500 and r["roti"]["grams"] == 120 and r["lassi"]["grams"] == 250 and "dal" in r
    assert res["unknown"] == []


def test_compound_dish_keeps_both_foods():
    r, res = by("rajma chawal")
    assert set(r) == {"rajma", "white rice"} and res["low_confidence"]
    assert set(by("dal chawal")[0]) == {"dal", "white rice"}


def test_leftover_words_are_unknown_not_dropped():
    r, res = by("2 egg bhurji")
    assert "egg" in r and res["unknown"] == ["bhurji"] and res["low_confidence"]


def test_confidence_flags():
    assert not parse_meal_text("2 eggs, 150g chicken breast and 1 cup rice")["low_confidence"]
    assert not parse_meal_text("rice")["low_confidence"]  # single item, default serving is fine
    assert parse_meal_text("rice, dal, sabzi")["low_confidence"]  # multi-item, no amounts
    assert parse_meal_text("400 rice")["low_confidence"]  # bare big number: ambiguous
    assert parse_meal_text("200g quinoa surprise")["unknown"]


# ---------------- sanitizing ----------------
def test_sanitize_db_food_uses_db_macros_at_llm_grams():
    it = meal_log.sanitize_item({"name": "Paneer tikka", "matches_db_name": "paneer", "grams": 250, "kcal": 1, "protein": 1, "carbs": 1, "fat": 1})
    assert it["name"] == "paneer" and it["estimated"] is False
    assert it["kcal"] == macros(FOODS["paneer"], 250)["kcal"] and it["grams"] == 250


def test_sanitize_unknown_food_flagged_and_clamped():
    it = meal_log.sanitize_item({"name": "quinoa bowl", "grams": 99999, "kcal": 400, "protein": 14, "carbs": 60, "fat": 10})
    assert it["estimated"] is True and it["grams"] == 3000
    assert it["kcal"] == 400  # within 25% of 4/4/9 (386): kept


def test_sanitize_inconsistent_kcal_recomputed_from_macros():
    it = meal_log.sanitize_item({"name": "mystery", "grams": 100, "kcal": 9000, "protein": 10, "carbs": 10, "fat": 10})
    assert it["kcal"] == 170 and it["estimated"] is True
    it = meal_log.sanitize_item({"name": "mystery", "grams": 100, "kcal": 170, "protein": 10, "carbs": 10, "fat": 10})
    assert it["kcal"] == 170
    it = meal_log.sanitize_item({"name": "mystery", "grams": -5, "protein": 10, "carbs": 10, "fat": 10})
    assert it["grams"] == 1 and it["kcal"] == 170  # kcal missing -> from macros


def test_sanitize_rejects_junk():
    assert meal_log.sanitize_item({"name": "", "kcal": 1}) is None
    assert meal_log.sanitize_item({"name": "air", "kcal": 0, "protein": 0, "carbs": 0, "fat": 0}) is None
    assert meal_log.sanitize_item("nope") is None


# ---------------- log_meal hybrid ----------------
def test_clean_text_makes_zero_llm_calls(llm):
    out = meal_log.log_meal("2 eggs, 150g chicken breast and 1 cup rice")
    assert llm.calls == [] and len(out["items"]) == 3 and out["unknown"] == []


def test_low_confidence_goes_to_llm_and_db_is_truth(llm):
    llm.reply = {"items": [
        {"name": "rajma", "matches_db_name": "rajma", "grams": 200, "kcal": 999, "protein": 1, "carbs": 1, "fat": 1},
        {"name": "steamed rice", "grams": 180, "kcal": 234, "protein": 5, "carbs": 50, "fat": 1}]}
    out = meal_log.log_meal("rajma chawal")
    assert len(llm.calls) == 1 and llm.calls[0] == "rajma chawal"
    rj = next(i for i in out["items"] if i["name"] == "rajma")
    assert rj["grams"] == 200 and rj["kcal"] == macros(FOODS["rajma"], 200)["kcal"] and not rj["estimated"]
    assert any(i["name"] == "white rice" and i["grams"] == 180 for i in out["items"])  # alias resolved to the DB entry
    assert out["unknown"] == [] and set(out) == {"items", "unknown"}


def test_unknown_food_estimated_by_llm_and_flagged(llm):
    llm.reply = {"items": [{"name": "quinoa surprise", "grams": 200, "kcal": 240, "protein": 9, "carbs": 40, "fat": 5}]}
    out = meal_log.log_meal("200g quinoa surprise")
    assert out["items"][0]["estimated"] is True and out["unknown"] == []
    assert db.get_meals(__import__("datetime").date.today().isoformat())[0]["estimated"]


@pytest.mark.parametrize("reply", [RuntimeError("boom"), {"items": []}, {}, {"items": [{"name": "", "kcal": 1}]}, None])
def test_llm_failure_falls_back_to_offline(llm, reply):
    llm.reply = reply if reply is not None else (lambda: None)
    out = meal_log.log_meal("200g quinoa surprise")
    assert out["items"] == [] and out["unknown"]  # unknown returned, not guessed
    out = meal_log.log_meal("rajma chawal")
    assert {i["name"] for i in out["items"]} == {"rajma", "white rice"}  # offline result kept


def test_llm_timeout_falls_back(llm, monkeypatch):
    monkeypatch.setattr(meal_log, "LLM_TIMEOUT_S", 0.05)
    llm.reply = lambda: time.sleep(0.5) or {"items": [{"name": "x", "grams": 1, "kcal": 1, "protein": 0, "carbs": 0, "fat": 0}]}
    t = time.time()
    out = meal_log.log_meal("rajma chawal")
    assert time.time() - t < 0.4 and {i["name"] for i in out["items"]} == {"rajma", "white rice"}


def test_no_key_never_calls_llm(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "n.db"))
    called = []
    monkeypatch.setattr(meal_log, "extract_structured", lambda *a, **k: called.append(1) or {"items": []})
    out = meal_log.log_meal("200g quinoa surprise, rajma chawal")
    assert called == [] and out["unknown"]
