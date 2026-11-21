import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.memory import db
from backend.tools.demo import PROFILE
from backend.tools.foods import parse_meal_text, suggest_for_protein


def names(r):
    return {i["name"]: i for i in r["items"]}


def test_parse_counts_and_grams():
    r = names(parse_meal_text("2 eggs, 150g chicken breast and 1 cup rice"))
    assert r["egg"]["grams"] == 100 and r["egg"]["protein"] == 12.6  # 2 eggs = 100 g at 12.6 g protein/100 g
    assert r["chicken breast"]["grams"] == 150 and r["chicken breast"]["protein"] == 46.5
    assert r["white rice"]["grams"] == 150


def test_parse_number_words_and_indian_foods():
    r = names(parse_meal_text("three roti with dal and paneer"))
    assert r["roti"]["grams"] == 120
    assert "dal" in r and "paneer" in r


def test_unknown_food_is_reported_not_guessed():
    r = parse_meal_text("200g quinoa surprise")
    assert r["items"] == [] and r["unknown"]


def test_longest_alias_wins():
    assert "sweet potato" in names(parse_meal_text("200g sweet potato"))


def test_suggestions_fit_budget():
    s = suggest_for_protein(40, 400)
    assert s and all(x["kcal"] <= 400 for x in s)
    assert suggest_for_protein(2, 500) == [] and suggest_for_protein(40, 30) == []


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return TestClient(app)


def test_endpoints_roundtrip(client):
    client.post("/onboard", json=PROFILE)
    client.post("/meal", json={"transcript": "4 eggs and 2 slices of bread"})
    s = client.get("/nutrition/today").json()
    assert len(s["meals"]) == 2 and s["eaten"]["protein"] > 25
    assert s["remaining"]["kcal"] < s["target"]["kcal"]
    client.delete(f"/meal/{s['meals'][0]['id']}")
    assert len(client.get("/nutrition/today").json()["meals"]) == 1


# ---------------- redesign support: any day, week strip, recent foods ----------------
def test_log_and_read_a_specific_day(client):
    client.post("/onboard", json=PROFILE)
    client.post("/meal", json={"transcript": "3 roti with dal", "day": "2024-05-01"})
    assert client.get("/nutrition/today").json()["meals"] == []                       # not today
    d = client.get("/nutrition/day", params={"day": "2024-05-01"}).json()
    assert d["day"] == "2024-05-01" and {m["name"] for m in d["meals"]} == {"roti", "dal"} and d["eaten"]["kcal"] > 0


def test_bad_day_is_rejected_cleanly(client):
    assert client.get("/nutrition/day", params={"day": "yesterday"}).status_code == 400
    assert client.post("/meal", json={"transcript": "an egg", "day": "31-12-2024"}).status_code == 400


def test_week_strip_is_seven_days_oldest_first_with_totals(client):
    from datetime import date, timedelta
    client.post("/onboard", json=PROFILE)
    client.post("/meal", json={"transcript": "4 eggs"})
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    client.post("/meal", json={"transcript": "200g chicken breast", "day": yesterday})
    w = client.get("/nutrition/week").json()
    assert len(w) == 7 and w[-1]["day"] == date.today().isoformat() and w[-2]["day"] == yesterday
    assert [x["day"] for x in w] == sorted(x["day"] for x in w)
    assert w[-1]["kcal"] > 0 and w[-1]["meals"] == 1 and w[-2]["protein"] > 50 and w[0]["kcal"] == 0
    assert all(x["target_kcal"] for x in w)


def test_recent_foods_ranked_by_how_often_you_eat_them_and_repeatable(client):
    from datetime import date, timedelta
    client.post("/onboard", json=PROFILE)
    for i in range(3):
        client.post("/meal", json={"transcript": "2 eggs", "day": (date.today() - timedelta(days=i + 1)).isoformat()})
    client.post("/meal", json={"transcript": "200g paneer"})
    rec = client.get("/meal/recent").json()
    assert rec[0]["name"] == "egg" and rec[0]["n"] == 3 and rec[1]["name"] == "paneer"
    assert client.post("/meal/repeat", json={"id": rec[0]["id"]}).status_code == 200
    today = client.get("/nutrition/today").json()
    assert [m["name"] for m in today["meals"]].count("egg") == 1 and today["meals"][-1]["kcal"] == rec[0]["kcal"]
    assert client.post("/meal/repeat", json={"id": 99999}).status_code == 404
