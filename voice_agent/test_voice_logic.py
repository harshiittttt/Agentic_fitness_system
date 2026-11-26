"""The pure logic of the voice agent (no GPU, no phone): what counts as a command, the speech-to-text safety net, Hinglish detection, phrase splitting.
    D:\\voice-proto\\venv\\Scripts\\python -m pytest voice_agent/test_voice_logic.py -q"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pytest  # noqa: E402

import server as S  # noqa: E402
from faster_whisper.transcribe import Segment  # noqa: E402


@pytest.mark.parametrize("text,follow,kind", [
    ("Copy.", False, "idle"), ("Right.", False, "idle"), ("Okay.", False, "idle"), ("Hello.", False, "hello"), ("Hey coach", False, "hello"),
    ("Boxing, wipes hands, adjust straps.", False, "stage"), ("Chalks up and grunts.", False, "stage"),
    ("60 kilos 5 reps", False, "pass"), ("Okay.", True, "pass"), ("Yes", True, "pass"), ("Skip this exercise", False, "pass"), ("What is next?", False, "pass"),
    ("I need to stretch my hamstrings first", False, "pass"), ("Let's stretch", False, "pass"), ("next set", False, "pass"), ("wrap up", False, "pass"),
])
def test_what_is_a_command(text, follow, kind):
    assert S.classify_turn(text, follow) == kind


@pytest.mark.parametrize("said,fixed", [
    ("Bhai, maini squat ka ek sit mar dia.", "Bhai, maine squat ka ek set maar diya."), ("I did gobble squads", "I did goblet squat"), ("dead lift 60 for 5", "deadlift 60 for 5"),
    ("vazhan kitna", "vajan kitna"), ("chahe reps huye", "chhe reps huye"), ("leg girl", "leg curl"), ("10 raps", "10 reps"), ("a nice bench press", "a nice bench press"),
])
def test_known_mishearings_are_repaired(said, fixed):
    assert S.fix_stt(said) == fixed


@pytest.mark.parametrize("text,hinglish", [
    ("Agla exercise kaunsa hai?", True), ("Kitne set baki hain?", True), ("Aaj ka plan kya hai?", True), ("आज का प्लान क्या है", True),
    ("60 kilos 5 reps done", False), ("What is the plan for today?", False), ("Do I need to focus on anything", False), ("let's do it", False),
])
def test_hinglish_detection(text, hinglish):
    assert S.is_hinglish(text) is hinglish


def test_long_sentences_are_spoken_in_phrases():
    line = "Lower A today: squat, romanian deadlift, leg extension, leg curl and calf raise."
    parts = S.chunks(line)
    assert len(parts) >= 2 and " ".join(parts) == line and all(len(p.split()) <= 11 for p in parts)
    assert S.chunks("Good set.") == ["Good set."]
    assert S.chunks("Nice work Golu. Same weight again.") == ["Nice work Golu.", "Same weight again."]


def test_a_real_command_that_is_in_the_prompt_is_not_a_prompt_echo():
    assert not S.is_prompt_echo("Agla exercise kaunsa hai?")        # the prompt contains this phrase on purpose
    assert not S.is_prompt_echo("bench press")
    assert S.is_prompt_echo("Squat deadlift bench press goblet squat leg curl leg extension lat pulldown")


def test_the_coachs_own_voice_is_not_the_user():
    said = "Solid set. Keep 60 on the bar for the next one."
    assert S.is_echo("solid set keep 60 on the bar", said, 1.0)
    assert not S.is_echo("how many sets are left", said, 1.0)


# ---- the speech stage's confidence rules, with a fake Whisper ----
def seg(text, no_speech=0.05, logprob=-0.3, comp=0.8):
    return Segment(id=0, seek=0, start=0.0, end=1.0, text=text, tokens=[], avg_logprob=logprob, compression_ratio=comp, no_speech_prob=no_speech, words=None, temperature=0.0)


class FakeModel:
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def transcribe(self, audio, **kw):
        self.calls.append(kw)
        segs, lang = self.results.pop(0)
        return iter(segs), SimpleNamespace(language=lang, language_probability=0.9)


def run(*results, **kw):
    m = FakeModel(*results)
    out, _ = S._PromptedWhisper(m).transcribe([0.0] * 16000, **kw)
    return [s.text for s in out], m


def test_a_real_sentence_followed_by_a_pause_is_kept():
    # Whisper reported no_speech_prob 0.71-0.81 for real, clearly transcribed speech that was followed by silence; the old rule (>= 0.4) threw it away
    texts, _ = run(([seg("We are not skipping anything.", no_speech=0.78, logprob=-0.35)], "en"))
    assert texts == ["We are not skipping anything."]


def test_silence_and_noise_are_dropped():
    assert run(([seg("Thank you for watching.", no_speech=0.2)], "en"))[0] == []
    assert run(([seg("Squat deadlift bench press leg curl", no_speech=0.8, logprob=-0.9)], "en"))[0] == []           # the prompt repeated back on silence
    assert run(([seg("blah", no_speech=0.9, logprob=-1.2)], "en"))[0] == []


def test_a_mumble_becomes_say_that_again_not_a_guess():
    texts, _ = run(([seg("Kike mech kya", no_speech=0.1, logprob=-1.4)], "hi"))
    assert texts == [S.UNCLEAR]


def test_mistaken_language_is_retried_in_english():
    texts, m = run(([seg("Sixty kilos for fife", logprob=-0.4)], "cy"), ([seg("Sixty kilos for five.", logprob=-0.2)], "en"))
    assert texts == ["Sixty kilos for five."] and m.calls[1]["language"] == "en"


def test_hindi_is_not_retried():
    texts, m = run(([seg("Agla exercise kaunsa hai?", logprob=-0.2)], "hi"))
    assert texts == ["Agla exercise kaunsa hai?"] and len(m.calls) == 1


# ---- Phase 2: the half-said set, what was heard of an interrupted answer, the Conversation Core stream ----
@pytest.mark.parametrize("text,frag", [
    ("Sixty kilos", True), ("Sixty kilos.", True), ("sixty", True), ("60", True), ("I did sixty kilos and", True), ("sixty for", True), ("Sixty two point five", True),
    ("Sixty kilos five reps", False), ("sixty for five", False), ("60 x 8", False), ("I did sixty kilos for five", False), ("sixty five reps", False),
    ("What is the next exercise?", False), ("how many sets are left", False), ("Okay, next set.", False), ("do ruko", False), ("saath kilo", True),
])
def test_half_said_sets_are_held(text, frag):
    assert S.numeric_fragment(text) is frag


def test_spoken_prefix_is_what_was_played():
    items = [["", 0.4], ["Five at sixty.", 1.0], ["Next up is Romanian deadlift, keep your back flat.", 3.0]]
    assert S.spoken_prefix(items, 0.2) == ""                            # still the acknowledgement clip
    assert S.spoken_prefix(items, 1.5) == "Five at sixty."              # clip 0.4 s + first sentence 1.0 s: heard whole
    part = S.spoken_prefix(items, 2.9)                                   # cut inside the second sentence: only its first words
    assert part.startswith("Five at sixty. Next") and "flat" not in part
    assert S.spoken_prefix(items, 10) == "Five at sixty. Next up is Romanian deadlift, keep your back flat."


def _fake_core(lines, status=200):
    import httpx

    def handler(req):
        return httpx.Response(status, content=("\n".join(lines) + "\n").encode(), headers={"content-type": "application/x-ndjson"})
    return httpx.AsyncClient(base_url="http://core", transport=httpx.MockTransport(handler))


def test_convo_run_reads_the_stream_and_notes_writes():
    import asyncio
    import json as _j

    async def go():
        evs = [{"t": "ack", "kind": "got_it"}, {"t": "tool", "name": "log_set", "status": "ok"}, {"t": "say", "i": 0, "text": "Five at sixty."}, {"t": "done", "intent": "set", "said": "Five at sixty."}]
        run = S.ConvoRun(_fake_core([_j.dumps(e) for e in evs]), {"turn_id": "t1", "text": "sixty for five"}, "converse")
        got = []
        while True:
            ev = await run.q.get()
            if ev is None:
                break
            got.append(ev["t"])
        return got, run.wrote

    got, wrote = asyncio.run(go())
    assert got == ["ack", "tool", "say", "done"] and wrote is True


def test_convo_run_reports_an_unreachable_core():
    import asyncio

    async def go():
        run = S.ConvoRun(_fake_core([], status=404), {"turn_id": "t2", "text": "hello"}, "converse")
        return await run.q.get()

    assert asyncio.run(go())["t"] == "_http_error"
