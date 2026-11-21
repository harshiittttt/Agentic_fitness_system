import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.memory import db
from backend.tools.foods import FOODS


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return TestClient(app)


def test_lookup_known_alias_and_unknown(client):
    r = client.get("/foods/portion", params={"name": "EGG"}).json()
    assert r["known"] and r["unit_g"] == FOODS["egg"]["unit_g"] and r["per100"]["protein"] == FOODS["egg"]["p"]
    alias = FOODS["egg"]["aliases"][-1]
    assert client.get("/foods/portion", params={"name": alias}).json()["name"] == "egg"
    u = client.get("/foods/portion", params={"name": "zzz thing"})
    assert u.status_code == 200 and u.json()["known"] is False and u.json()["per100"] is None and u.json()["unit"] is None


def test_log_exact_grams_and_patch_in_place(client):
    r = client.post("/meal/portion", json={"name": "chicken breast", "grams": 150, "day": "2026-01-05"}).json()
    mid = r["id"]
    assert r["item"]["protein"] == 46.5 and r["item"]["grams"] == 150 and r["item"]["day"] == "2026-01-05"
    before = db.get_meal(mid)
    p = client.patch(f"/meal/{mid}/portion", json={"grams": 300}).json()
    assert p["id"] == mid and p["day"] == "2026-01-05" and p["ts"] == before["ts"]
    assert p["grams"] == 300 and p["protein"] == 93.0
    assert len(db.get_meals("2026-01-05")) == 1


def test_units(client):
    mid = client.post("/meal/portion", json={"name": "egg", "grams": 50}).json()["id"]
    p = client.patch(f"/meal/{mid}/portion", json={"units": 3}).json()
    assert p["grams"] == 3 * FOODS["egg"]["unit_g"]


def test_estimated_row_scales_proportionally(client):
    mid = db.add_meal("2026-01-05", "mystery curry", 200, 400, 20, 40, 10, True)
    p = client.patch(f"/meal/{mid}/portion", json={"grams": 100}).json()
    assert p["id"] == mid and p["kcal"] == 200 and p["protein"] == 10 and p["carbs"] == 20 and p["fat"] == 5
    assert client.patch(f"/meal/{mid}/portion", json={"units": 2}).status_code == 400


def test_no_grams_row_400(client):
    mid = db.add_meal("2026-01-05", "mystery curry", None, 400, 20, 40, 10, True)
    r = client.patch(f"/meal/{mid}/portion", json={"grams": 100})
    assert r.status_code == 400 and "grams" in r.json()["detail"]


def test_bounds_and_missing(client):
    mid = client.post("/meal/portion", json={"name": "egg", "grams": 50}).json()["id"]
    for g in (0, 0.5, 5001, -3):
        assert client.patch(f"/meal/{mid}/portion", json={"grams": g}).status_code in (400, 422)
    assert client.patch(f"/meal/{mid}/portion", json={}).status_code == 422
    assert client.patch("/meal/9999/portion", json={"grams": 100}).status_code == 404
    assert db.get_meal(mid)["grams"] == 50
    assert client.post("/meal/portion", json={"name": "egg", "grams": 9000}).status_code in (400, 422)


def test_post_unknown_food_and_bad_day(client):
    r = client.post("/meal/portion", json={"name": "zzz thing", "grams": 100})
    assert r.status_code == 404 and r.json()["detail"] == "unknown food"
    assert client.post("/meal/portion", json={"name": "egg", "grams": 50, "day": "nope"}).status_code == 400


def test_portion_mode_count_vs_weight(client):
    egg = client.get("/foods/portion", params={"name": "eggs"}).json()
    rice = client.get("/foods/portion", params={"name": "rice"}).json()
    milk = client.get("/foods/portion", params={"name": "milk"}).json()
    roti = client.get("/foods/portion", params={"name": "roti"}).json()
    unknown = client.get("/foods/portion", params={"name": "mystery stew"}).json()
    assert egg["mode"] == "count" and roti["mode"] == "count"
    assert rice["mode"] == "weight" and rice["measure"] == "g"
    assert milk["mode"] == "weight" and milk["measure"] == "ml"
    assert unknown["mode"] == "weight"


def test_preview_inserts_nothing_and_flags_needs_amount(client):
    before = len(db.get_meals("2026-01-05"))
    r = client.post("/meal/preview", json={"text": "eggs and rice", "day": "2026-01-05"}).json()
    assert len(db.get_meals("2026-01-05")) == before
    by = {i["name"]: i for i in r["items"]}
    assert r["needs_amount"] is True and not by["egg"]["qty_given"]
    assert by["egg"]["mode"] == "count" and by["egg"]["unit"] == "egg" and by["egg"]["unit_g"]
    assert by["white rice"]["mode"] == "weight" and by["white rice"]["measure"] == "g"


def test_preview_all_given_no_amount_needed(client):
    r = client.post("/meal/preview", json={"text": "4 eggs and 400g rice"}).json()
    assert r["needs_amount"] is False and [i["grams"] for i in r["items"]][1] == 400
    assert client.post("/meal/preview", json={"text": "milk"}).json()["items"][0]["measure"] == "ml"
    assert client.post("/meal/preview", json={"text": "x", "day": "bad"}).status_code == 400
