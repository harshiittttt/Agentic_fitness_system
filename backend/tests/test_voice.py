from backend.tools.voice_parser import _fallback


def test_simple_set():
    r = _fallback("bench press 80 for 8 felt easy")
    s = r["sets"][0]
    assert s["exercise"] == "bench press" and s["weight"] == 80 and s["reps"] == 8 and s["rpe"] == 6.0


def test_explicit_rpe():
    r = _fallback("squat 100 kg for 5 rpe 9")
    assert r["sets"][0]["rpe"] == 9.0


def test_no_sets():
    assert _fallback("just stretching today")["sets"] == []
