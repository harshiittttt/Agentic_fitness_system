"""Tests for the harness itself (no backend, no network, no GPU):  python -m pytest voice_agent/eval/test_eval.py -q"""
import random

import numpy as np
import pytest

from voice_agent.eval import generate, report, runner, scorers
from voice_agent.eval.audio import degrade, gym_noise
from voice_agent.eval.nums import numbers_in


# ---- numbers ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text,want", [
    ("sixty-two point five times eight", [62.5, 8]), ("a hundred and twenty kilos", [120]), ("saath kilo paanch reps", [60, 5]), ("60 for 5", [60, 5]),
    ("pachaas kilo chhe baar", [50, 6]), ("ninety nine", [99]), ("2.5 kg", [2.5]), ("sixty, no, sixty-five", [60, 65]), ("one hundred", [100]),
])
def test_numbers_in(text, want):
    assert numbers_in(text) == want


def test_ambiguous_one_is_dropped_for_replies():
    assert numbers_in("no one is here", include_ambiguous=False) == []
    assert numbers_in("one more set", include_ambiguous=False) == []
    assert numbers_in("one more set") == [1]


# ---- generator -------------------------------------------------------------------------------------------------------
def test_generator_sizes_and_unique_names():
    pos, neg = generate.positives(random.Random(7)), generate.negatives()
    assert len(pos) >= 100 and len(neg) >= 60 and len(generate.ambiguous()) == 20
    names = [s["name"] for s in pos + neg + generate.ambiguous() + generate.skips()]
    assert len(names) == len(set(names))


def test_generated_positive_expectations_match_the_words_spoken():
    for s in generate.positives(random.Random(7)):
        if "hindi" in s["tags"] or "correction" in s["tags"]:
            continue
        exp = s["turns"][0]["expect"]["sets"][0]
        said = numbers_in(s["turns"][0]["say"])
        assert exp["weight"] in said and exp["reps"] in said, (s["turns"][0]["say"], exp, said)


def test_every_negative_expects_no_action():
    for s in generate.negatives():
        e = s["turns"][0]["expect"]
        assert "sets" not in e and "skipped" not in e


def test_scenario_files_load_and_are_well_formed():
    scen = runner.load([])
    assert len(scen) >= 260
    for s in scen:
        assert s["turns"], s["name"]
        for t in s["turns"]:
            assert "say" in t or "say_parts" in t, s["name"]


# ---- scorers ---------------------------------------------------------------------------------------------------------
STATE = {"current": {"name": "squat"}, "exercises": [{"name": "squat", "sets": 3, "done": 0, "skipped": False, "reps": 5}]}
BEFORE = {"sets": [], "skipped": [], "state": STATE}


def after(sets=(), skipped=()):
    return {"sets": [{"id": i + 1, "exercise": e, "weight": w, "reps": r} for i, (e, w, r) in enumerate(sets)], "skipped": list(skipped), "state": STATE}


def score(expect, aft, reply="Good set.", said="sixty for five"):
    return scorers.score_turn({}, expect, BEFORE, aft, {"reply": reply, "intent": "set", "actions": []}, "en", said)


def test_right_set_passes_and_defaults_to_current_exercise():
    r = score({"sets": [{"weight": 60, "reps": 5}]}, after([("squat", 60, 5)]))
    assert not r["wrong_action"] and not r["miss"] and all(c["ok"] for c in r["checks"])


def test_wrong_values_are_a_wrong_action_not_just_a_miss():
    r = score({"sets": [{"weight": 60, "reps": 5}]}, after([("squat", 60, 8)]))
    assert r["wrong_action"] and r["miss"]


def test_set_under_a_made_up_exercise_is_wrong():
    assert score({"sets": [{"weight": 60, "reps": 5}]}, after([("unknown", 60, 5)]))["wrong_action"]


def test_negative_turn_that_logs_or_skips_is_a_wrong_action():
    assert score({}, after([("squat", 60, 5)]))["wrong_action"]
    assert score({}, after(skipped=["squat"]))["wrong_action"]
    assert not score({}, after())["wrong_action"]


def test_expected_skip_and_missing_skip():
    assert not score({"skipped": ["squat"]}, after(skipped=["squat"]))["miss"]
    r = score({"skipped": ["squat"]}, after())
    assert r["miss"] and not r["wrong_action"]


def test_grounding_flags_an_invented_number():
    ok = score({"sets": [{"weight": 60, "reps": 5}]}, after([("squat", 60, 5)]), reply="Same again, 60 for 5.")
    assert not ok["ungrounded"]
    bad = score({"sets": [{"weight": 60, "reps": 5}]}, after([("squat", 60, 5)]), reply="Nice, that is your 14th rep of the week at 72 kilos.")
    assert 72.0 in bad["ungrounded"] and not all(c["ok"] for c in bad["checks"])


def test_devanagari_reply_to_roman_speaker_fails():
    r = score({}, after(), reply="ठीक है, आगे बढ़ते हैं", said="agla exercise kaunsa hai")
    assert any(c["name"] == "script" and not c["ok"] for c in r["checks"])


def test_asks_check():
    assert all(c["ok"] for c in score({"asks": True}, after(), reply="Sixty for how many reps?")["checks"])
    assert not all(c["ok"] for c in score({"asks": True}, after(), reply="Logged it.")["checks"])


def test_percentile_and_wer():
    assert scorers.percentile([1, 2, 3, 4, 5], 50) == 3
    assert scorers.percentile([], 50) is None
    assert scorers.wer("sixty for five", "sixty for five") == 0
    assert scorers.wer("sixty for five", "sixty fork five") == pytest.approx(1 / 3)
    assert scorers.wer("Okay, next set.", "Ok next set") == 0 and scorers.wer("Sixty kilos, five reps", "60 kilos 5 reps") == 0


# ---- report ----------------------------------------------------------------------------------------------------------
def rec(ok=True, wrong=False, tags=("negative",), fw=900):
    return {"scenario": "s", "turn": 0, "say": "x", "tags": list(tags), "ok": ok, "wrong_action": wrong, "checks": [], "first_word_ms": fw, "ungrounded": []}


def test_summary_counts_wrong_actions_and_latency():
    m = report.summarise([rec(), rec(ok=False, wrong=True, fw=7000), rec(fw=1000)])
    assert m["wrong_actions_neg"] == 1 and m["wrong_actions_all"] == 1 and m["over_6s"] == pytest.approx(33.3, abs=0.1)
    assert "FAIL" in report.scorecard({"cfg": m})


# ---- audio helpers ---------------------------------------------------------------------------------------------------
def test_noise_mix_hits_requested_snr_roughly_and_lowpass_is_quieter():
    t = np.arange(16000) / 16000
    speech = (np.sin(2 * np.pi * 300 * t) * 8000).astype("<i2").tobytes()
    noisy = np.frombuffer(degrade(speech, 5.0, False), "<i2").astype(float)
    clean = np.frombuffer(speech, "<i2").astype(float)
    snr = 10 * np.log10(np.mean(clean ** 2) / np.mean((noisy - clean) ** 2))
    assert 3.5 < snr < 6.5
    far = np.frombuffer(degrade(speech, None, True), "<i2").astype(float)
    assert np.sqrt(np.mean(far ** 2)) < 0.5 * np.sqrt(np.mean(clean ** 2))
    assert gym_noise(16000, np.random.default_rng(1)).shape == (16000,)


def test_false_log_claim_detected():
    assert scorers.false_log_claim("Six reps at seventy two point five logged, Golu.", [])
    assert not scorers.false_log_claim("Six reps at 60 logged, Golu.", [("squat", 60, 6)])
    assert not scorers.false_log_claim("Nothing was logged today, Golu.", [])
    assert not scorers.false_log_claim("What weight was that?", [])


def test_wer_scoring_skips_ignored_lines_and_scores_hindi_apart():
    from voice_agent.eval import report
    base = {"scenario": "s", "ok": True, "checks": [], "tags": []}
    recs = [
        {**base, "lang": "en", "say": "Sixty kilos, five reps", "heard": "60 kilos, 5 reps", "wer": 0.0, "first_sound_ms": 500},
        {**base, "lang": "en", "say": "No wait, put it back", "heard": "No weight, put it back", "wer": 0.2, "first_sound_ms": 500},
        {**base, "lang": "en", "say": "Copy.", "heard": "", "wer": 1.0, "first_sound_ms": None},                     # ignored on purpose: the agent sends no transcript
        {**base, "lang": "hinglish", "say": "saath kilo paanch reps hogaya", "heard": "Saath kilo paanch reps haageya.", "wer": 0.2, "first_sound_ms": 500},
        {**base, "lang": "hinglish", "say": "\u092c\u0938 \u0924\u094b \u0928\u093e", "heard": "Bas to na", "wer": 1.0, "first_sound_ms": 500},  # Devanagari reference vs Roman transcript
    ]
    m = report.summarise(recs)
    assert m["wer_n"] == 2 and m["wer"] == pytest.approx(10.0)
    assert m["wer_hinglish_n"] == 1
