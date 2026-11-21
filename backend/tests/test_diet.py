"""The diet consultant: the planner's numbers are right and respect preferences, preferences persist, swaps work, and the agent's tools
(not the model) produce every quantity. The model is faked, so everything here is offline."""
import pytest
from fastapi.testclient import TestClient

from backend.agents import orchestrator as orch
from backend.diet import agent, planner
from backend.main import app
from backend.memory import db
from backend.tools import demo
from backend.tools.foods import FOODS

T = {"kcal": 2600, "protein": 140, "carbs": 300, "fat": 70}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    orch.onboard({**demo.PROFILE, "name": "Golu", "goal": "bulk"})
    return tmp_path


# ------------------------------------------------------------------ planner
@pytest.mark.parametrize("kcal,prot", [(1500, 90), (1800, 110), (2200, 120), (2600, 140), (3200, 170)])
@pytest.mark.parametrize("dt", ["veg", "egg", "nonveg"])
@pytest.mark.parametrize("meals", [3, 4, 5])
def test_every_day_of_the_week_lands_near_the_targets(kcal, prot, dt, meals):
    w = planner.plan_week({"kcal": kcal, "protein": prot, "carbs": 0, "fat": 0}, {"diet_type": dt, "meals_per_day": meals})
    assert len(w["days"]) == 7
    for d in w["days"]:
        assert abs(d["delta"]["kcal"]) <= 0.14 * kcal, d["delta"]
        assert abs(d["delta"]["protein"]) <= 0.14 * prot, d["delta"]
        for m in d["meals"]:
            for i in m["items"]:
                assert i["grams"] > 0 and i["label"] and i["food"] in FOODS


def test_totals_are_the_sum_of_the_rounded_items():
    d = planner.plan_day(T, {"diet_type": "egg", "meals_per_day": 3})
    for m in d["meals"]:
        assert m["totals"]["kcal"] == round(sum(i["kcal"] for i in m["items"]))
        assert m["totals"]["protein"] == pytest.approx(sum(i["protein"] for i in m["items"]), abs=0.2)
    assert d["totals"]["protein"] == pytest.approx(sum(m["totals"]["protein"] for m in d["meals"]), abs=0.3)


def test_veg_never_gets_egg_or_meat_and_egg_never_gets_meat():
    meat = {"chicken breast", "fish", "beef", "salmon"}
    for dt, banned in (("veg", meat | {"egg", "egg white"}), ("egg", meat)):
        w = planner.plan_week(T, {"diet_type": dt, "meals_per_day": 5})
        used = {i["food"] for d in w["days"] for m in d["meals"] for i in m["items"]}
        assert not (used & banned), (dt, used & banned)


def test_dislikes_and_allergies_are_excluded_everywhere():
    w = planner.plan_week(T, {"diet_type": "veg", "meals_per_day": 4, "dislikes": ["rajma", "paneer"], "allergies": ["peanut"]})
    used = {i["food"] for d in w["days"] for m in d["meals"] for i in m["items"]}
    assert not (used & {"rajma", "paneer", "peanuts", "peanut butter"})
    w = planner.plan_week(T, {"diet_type": "veg", "meals_per_day": 3, "allergies": ["milk"]})
    used = {i["food"] for d in w["days"] for m in d["meals"] for i in m["items"]}
    assert not (used & {"milk", "curd", "paneer", "whey protein"})


def test_weekly_plan_has_variety_and_a_grocery_list():
    w = planner.plan_week(T, {"diet_type": "nonveg", "meals_per_day": 3})
    titles = {m["title"] for d in w["days"] for m in d["meals"]}
    assert len(titles) >= 8
    assert w["grocery"] and all(g["grams"] > 0 and g["buy"] for g in w["grocery"])


def test_portions_are_believable():
    for dt in ("veg", "nonveg"):
        for d in planner.plan_week(T, {"diet_type": dt, "meals_per_day": 3})["days"]:
            for m in d["meals"]:
                for i in m["items"]:
                    if i["food"] == "paneer":
                        assert i["grams"] <= 170
                    if i["food"] == "egg":
                        assert i["grams"] <= 50 * 6
                    if i["food"] == "roti":
                        assert i["grams"] <= 40 * 5


def test_swap_keeps_the_meal_and_protein():
    days = planner.plan_week(T, {"diet_type": "nonveg", "meals_per_day": 3})["days"]
    meal = next(m for d in days for m in d["meals"] if any(i["food"] == "chicken breast" for i in m["items"]))
    new = planner.swap_food(meal, "chicken", {"diet_type": "nonveg"}, "paneer")
    assert new and any(i["food"] == "paneer" for i in new["items"]) and not any(i["food"] == "chicken breast" for i in new["items"])
    assert new["totals"]["protein"] == pytest.approx(meal["totals"]["protein"], abs=6)
    assert planner.swap_food(meal, "chicken", {"diet_type": "veg"}, "fish") is None     # a vegetarian is never handed fish


def test_clean_prefs_rejects_junk():
    p = planner.clean_prefs({"diet_type": "carnivore", "meals_per_day": 9, "dislikes": "okra, karela", "budget": "lavish"})
    assert p["diet_type"] == "veg" and p["meals_per_day"] == 3 and p["dislikes"] == ["okra", "karela"] and p["budget"] == "medium"


# ------------------------------------------------------------------ preferences + routes
def test_preferences_persist_and_merge(env):
    assert agent.get_prefs()["saved"] is False and set(agent._missing()) == {"diet_type", "meals_per_day"}
    agent.save_prefs({"diet_type": "nonveg", "dislikes": ["lauki"]})
    agent.save_prefs({"meals_per_day": 4, "dislikes": ["karela"], "diet_type": "robot"})   # junk value is ignored, lists merge
    p = agent.get_prefs()
    assert p["diet_type"] == "nonveg" and p["meals_per_day"] == 4 and p["dislikes"] == ["karela", "lauki"]
    assert agent._missing() == []


def test_routes_state_prefs_plan_week_swap(env):
    with TestClient(app) as c:
        s = c.get("/diet/state").json()
        assert s["needs"] and s["targets"]["kcal"] > 0 and s["plan"] is None
        assert c.post("/diet/prefs", json={"diet_type": "egg", "meals_per_day": 4}).json()["diet_type"] == "egg"
        assert c.post("/diet/prefs", json={"diet_type": "robot"}).status_code == 422
        day = c.get("/diet/plan?variation=2").json()
        assert len(day["meals"]) >= 4 and abs(day["delta"]["kcal"]) < 0.15 * day["target"]["kcal"]
        assert c.get("/diet/state").json()["plan"]["variation"] == 2          # the plan on screen is remembered
        wk = c.get("/diet/week").json()
        assert len(wk["days"]) == 7 and wk["grocery"]
        food = day["meals"][1]["items"][0]["food"]
        r = c.post("/diet/swap", json={"slot": day["meals"][1]["slot"], "food": food})
        assert r.status_code in (200, 409)
        assert c.post("/diet/swap", json={"slot": "Lunch", "food": "unicorn"}).status_code == 409


# ------------------------------------------------------------------ the agent
def test_chat_without_a_key_says_so_plainly(env):
    r = agent.chat("what should I eat?")
    assert "key" in r["reply"].lower() and r["needs"]


def test_agent_tools_build_the_plan_and_the_reply_carries_it(env, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    seen = {}

    def fake(system, user, tools, handlers, max_turns=6, max_tokens=1024, history=None, trace=None):
        seen["system"], seen["tools"] = system, [t["name"] for t in tools]
        handlers["save_diet_preferences"](diet_type="nonveg", meals_per_day=3)
        r = handlers["make_meal_plan"](variation=1)
        trace.append({"tool": "make_meal_plan", "args": {}, "result": r})
        return "Here is your day. Protein lands close to target."

    monkeypatch.setattr(agent, "run_agent", fake)
    out = agent.chat("tell me what to eat in a day, I eat eggs and chicken, 3 meals")
    assert out["reply"].startswith("Here is your day") and out["plan"]["meals"] and out["needs"] == []
    assert out["prefs"]["diet_type"] == "nonveg"
    assert "set_calories" in seen["tools"] and "make_week_plan" in seen["tools"]
    assert "never invent a quantity" in seen["system"].lower()
    hist = agent.history_for_app()
    assert [h["role"] for h in hist] == ["user", "assistant"]


def test_agent_replays_only_diet_turns_and_never_the_workout_chat(env, monkeypatch):
    from fitness_os import conversation
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    conversation.record("chat", "user", "how is my bench", learn=False)
    captured = {}

    def fake(system, user, tools, handlers, max_turns=6, max_tokens=1024, history=None, trace=None):
        captured["history"] = history
        return "ok"

    monkeypatch.setattr(agent, "run_agent", fake)
    agent.chat("protein in 100g paneer?")
    agent.chat("and eggs?")
    assert all("bench" not in h["content"] for h in captured["history"]) and any("paneer" in h["content"] for h in captured["history"])


def test_eating_disorder_language_gets_the_fixed_serious_answer_not_the_model(env, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setattr(agent, "run_agent", lambda *a, **k: pytest.fail("the model must not answer this"))
    r = agent.chat("I want to starve myself and eat nothing for a week")
    assert r["reply"]
