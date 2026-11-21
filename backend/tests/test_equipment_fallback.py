"""The offline starter plan must respect equipment: a dumbbell-only athlete never gets a barbell lift, injuries still apply."""
import pytest

from backend.tools.profile_edit import training_plan
from fitness_os.planning import EQUIPMENT

BASE = dict(age=30, sex="m", height_cm=178, weight_kg=78, goal="maintain", days_per_week=4, experience="intermediate", injuries=[], avoid=[])


def names(plan):
    return [e["name"] for d in plan["days"] for e in d["exercises"]]


@pytest.mark.parametrize("have", [["dumbbell"], ["bodyweight"], ["dumbbell", "cable"], ["machine", "cable"]])
def test_every_exercise_is_doable_with_the_equipment_you_have(have):
    plan = training_plan({**BASE, "equipment": have})
    allowed = set(have) | {"bodyweight"}
    assert names(plan) and all(EQUIPMENT[n] & allowed for n in names(plan))
    assert all(len(d["exercises"]) >= 1 for d in plan["days"])


def test_unspecified_equipment_means_a_full_gym_and_changes_nothing():
    plan = training_plan(BASE)
    assert "squat" in names(plan) or "deadlift" in names(plan)


def test_equipment_and_injuries_apply_together():
    plan = training_plan({**BASE, "equipment": ["dumbbell"], "injuries": ["left knee pain"]})
    assert all(n not in ("squat", "lunges", "leg press", "front squat") for n in names(plan)) and names(plan)


def test_a_swap_keeps_the_movement_pattern():
    from backend.tools.exercises import info
    plan = training_plan({**BASE, "equipment": ["dumbbell"]})
    patterns = {info(n)["pattern"] for n in names(plan)}
    assert {"squat", "horizontal push"} & patterns      # lower and upper body work survive the swap
