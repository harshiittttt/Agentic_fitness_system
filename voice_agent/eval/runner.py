"""Replays scenarios against a brain (text mode) or the voice agent (audio mode) and collects one record per turn."""
from __future__ import annotations

import fnmatch
import glob
import time
from pathlib import Path

import yaml

from . import scorers
from .world import World

SCEN_DIR = Path(__file__).resolve().parent.parent / "scenarios"


def load(paths: list[str]) -> list[dict]:
    files: list[str] = []
    for p in paths or [str(SCEN_DIR / "*.yaml")]:
        hits = sorted(glob.glob(p)) or sorted(glob.glob(str(SCEN_DIR / p))) or sorted(glob.glob(str(SCEN_DIR / (p + ".yaml"))))
        if not hits:
            raise SystemExit(f"no scenario file matches {p!r}")
        files += hits
    out: list[dict] = []
    for f in dict.fromkeys(files):
        doc = yaml.safe_load(Path(f).read_text(encoding="utf-8")) or {}
        for s in doc.get("scenarios", []):
            s.setdefault("tags", [])
            s.setdefault("lang", "en")
            s["file"] = Path(f).name
            out.append(s)
    return out


def select(scenarios: list[dict], tags: list[str] | None, exclude: list[str] | None, name: str | None, limit: int | None, mode: str) -> list[dict]:
    out = []
    for s in scenarios:
        t = set(s["tags"])
        if mode == "text" and "audio_only" in t:
            continue
        if tags and not t & set(tags):
            continue
        if exclude and t & set(exclude):
            continue
        if name and not fnmatch.fnmatch(s["name"], name if "*" in name else f"*{name}*"):
            continue
        out.append(s)
    return out[:limit] if limit else out


def turn_text(turn: dict) -> str:
    return turn["say"] if "say" in turn else " ... ".join(turn.get("say_parts", []))


class TextExecutor:
    """Sends the transcript straight to the brain (no audio): tests the decision logic, not the ears."""

    def __init__(self, brain):
        self.brain = brain
        self.label = f"text:{brain.name}"

    def open_scenario(self, scen: dict) -> None:
        if hasattr(self.brain, "new_conversation"):
            self.brain.new_conversation()

    def close_scenario(self) -> None:
        pass

    def turn(self, turn: dict, lang: str | None) -> dict:
        res = self.brain.turn(turn_text(turn), lang=None if lang == "en" else lang)
        res["heard"] = turn_text(turn)
        res["first_sound_ms"] = None
        return res


def run(executor, world: World, scenarios: list[dict], progress=print) -> list[dict]:
    recs: list[dict] = []
    t_start = time.time()
    for n, scen in enumerate(scenarios, 1):
        world.reset()
        executor.open_scenario(scen)
        for ti, turn in enumerate(scen["turns"]):
            expect = turn.get("expect") or {}
            say = turn_text(turn)
            before = world.snapshot()
            rec = {"scenario": scen["name"], "file": scen.get("file"), "tags": scen["tags"], "turn": ti, "say": say, "lang": scen.get("lang"), "config": executor.label}
            try:
                res = executor.turn(turn, scen.get("lang"))
                after = world.snapshot()
                sc = scorers.score_turn(turn, expect, before, after, res, scen.get("lang"), res.get("heard") or say)
                rec.update(reply=res.get("reply"), intent=res.get("intent"), actions=res.get("actions"), ms=round(res.get("ms") or 0), first_word_ms=round(res["first_word_ms"]) if res.get("first_word_ms") else None,
                           first_sound_ms=round(res["first_sound_ms"]) if res.get("first_sound_ms") else None, heard=res.get("heard"), wer=res.get("wer"),
                           barge_stop_ms=res.get("barge_stop_ms"), **{k: sc[k] for k in ("checks", "wrong_action", "miss", "new_sets", "newly_skipped", "unskipped", "ungrounded")})
                if expect.get("barge_stop_ms_max") is not None:
                    bs = res.get("barge_stop_ms")
                    ok = bs is not None and bs <= expect["barge_stop_ms_max"]
                    rec["checks"].append({"name": "barge_stop_ms_max", "ok": ok, "detail": f"{bs} ms" if bs is not None else "never stopped / not measured"})
                rec["state_after"] = {"done": {e["name"]: e["done"] for e in after["state"].get("exercises", [])}, "skipped": after["skipped"]}
                rec["ok"] = not scorers.blocking(rec["checks"])
            except Exception as e:                                                # a crashed turn is a failed turn, never a silent skip
                rec.update(error=f"{type(e).__name__}: {e}", ok=False, checks=[{"name": "no_error", "ok": False, "detail": str(e)}], wrong_action=False, miss=True,
                           reply="", ms=0, first_word_ms=None, first_sound_ms=None, ungrounded=[], new_sets=[], newly_skipped=[], unskipped=[])
            recs.append(rec)
        executor.close_scenario()
        if progress and (n % 10 == 0 or n == len(scenarios)):
            bad = sum(1 for r in recs if not r["ok"])
            progress(f"  [{executor.label}] {n}/{len(scenarios)} scenarios, {len(recs)} turns, {bad} failing turns, {time.time() - t_start:.0f}s")
    return recs
