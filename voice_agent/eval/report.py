"""Turns per-turn records into the scorecard (targets from docs/CONVERSATIONAL_AGENT_PLAN.md section 6.2) and a markdown report."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

from .scorers import blocking, false_log_claim, percentile, wer

# metric -> (label, target text, test(value) -> bool)  ; value None = not measured
TARGETS = {
    "wrong_actions_neg": ("Wrong actions on negative cases (count)", "0", lambda v: v == 0),
    "wrong_actions_all": ("Wrong actions, all turns (count)", "0", lambda v: v == 0),
    "set_accuracy": ("Set-logging accuracy (positive set turns)", ">= 98%", lambda v: v >= 98),
    "correction_rate": ("Self-corrections resolved ('sixty, no, sixty-five')", ">= 90%", lambda v: v >= 90),
    "clarify_rate": ("Asks instead of acting on ambiguous cases", ">= 90%", lambda v: v >= 90),
    "false_claims": ("Replies claiming 'logged' when nothing was written (count)", "0", lambda v: v == 0),
    "ungrounded_turns": ("Replies with an untraceable number (count)", "0", lambda v: v == 0),
    "script_fail": ("Replies in Devanagari to a Roman-letter caller (count)", "0", lambda v: v == 0),
    "first_word_p50": ("First composed word p50 (ms)", "<= 1800", lambda v: v <= 1800),
    "first_word_p95": ("First composed word p95 (ms)", "<= 3000", lambda v: v <= 3000),
    "first_sound_p50": ("First sound p50 (ms, audio mode)", "<= 800", lambda v: v <= 800),
    "first_sound_p95": ("First sound p95 (ms, audio mode)", "<= 1500", lambda v: v <= 1500),
    "over_6s": ("Turns over 6 s (%)", "< 1%", lambda v: v < 1),
    "barge_stop": ("Barge-in: audio stops after speech starts (ms)", "<= 300-450", lambda v: v <= 450),
    "wer": ("Word error rate, English lines the agent passed on (%, audio mode)", "<= 8%", lambda v: v <= 8),
    "wer_hinglish": ("Word error rate, Hinglish lines (informational: Roman spelling varies)", "-", lambda v: True),
}


def _pct(a: int, b: int) -> float | None:
    return round(100 * a / b, 1) if b else None


def summarise(recs: list[dict]) -> dict:
    for r in recs:
        if r.get("checks") is not None:
            if not any(c["name"] == "no_false_log_claim" for c in r["checks"]) and false_log_claim(r.get("reply") or "", r.get("new_sets") or []):
                r["checks"].append({"name": "no_false_log_claim", "ok": False, "detail": f"reply claims a set was logged but none was: {r.get('reply')!r}"})
            r["ok"] = not blocking(r["checks"])
        if r.get("heard") is not None and r.get("first_sound_ms") is not None or r.get("wer") is not None:
            r["wer"] = wer(r["say"].replace(" ... ", " "), r.get("heard") or "")        # recomputed so older run files get the current normalisation
    tags = lambda r: set(r.get("tags") or [])        # noqa: E731
    neg = [r for r in recs if "negative" in tags(r)]
    setturns = [r for r in recs if any(c["name"] == "set_logged" for c in r.get("checks", []))]
    corr = [r for r in recs if "correction" in tags(r)]
    amb = [r for r in recs if "ambiguous" in tags(r)]
    fw = [r["first_word_ms"] for r in recs if r.get("first_word_ms")]
    fs = [r["first_sound_ms"] for r in recs if r.get("first_sound_ms")]
    barge = [r["barge_stop_ms"] for r in recs if r.get("barge_stop_ms") is not None]
    # Only a line the agent actually transcribed AND passed on has a "heard" text: ignored lines (copy, stage directions, "wait, stop", mumbles) send none and are not ASR errors.
    # Hinglish and Devanagari lines are scored apart: the same Hindi sentence has many correct Roman spellings, and a Devanagari reference can never equal a Roman transcript.
    def _hindi(r):
        return r.get("lang") == "hinglish" or bool(re.search(r"[ऀ-ॿ]", r.get("say") or ""))
    scored = [r for r in recs if r.get("wer") is not None and (r.get("heard") or "").strip()]
    wers = [r["wer"] for r in scored if not _hindi(r)]
    wers_hi = [r["wer"] for r in scored if _hindi(r) and not re.search(r"[ऀ-ॿ]", r.get("say") or "")]
    scen = defaultdict(list)
    for r in recs:
        scen[r["scenario"]].append(r["ok"])
    m = {
        "scenarios": len(scen), "scenarios_ok": sum(all(v) for v in scen.values()), "turns": len(recs), "turns_ok": sum(1 for r in recs if r["ok"]),
        "wrong_actions_neg": sum(1 for r in neg if r.get("wrong_action")), "wrong_actions_all": sum(1 for r in recs if r.get("wrong_action")),
        "set_accuracy": _pct(sum(1 for r in setturns if r["ok"]), len(setturns)), "set_turns": len(setturns),
        "correction_rate": _pct(sum(1 for r in corr if r["ok"]), len(corr)), "correction_n": len(corr),
        "clarify_rate": _pct(sum(1 for r in amb if r["ok"]), len(amb)), "clarify_n": len(amb),
        "false_claims": sum(1 for r in recs for c in r.get("checks", []) if c["name"] == "no_false_log_claim" and not c["ok"]),
        "ungrounded_turns": sum(1 for r in recs if r.get("ungrounded")),
        "script_fail": sum(1 for r in recs for c in r.get("checks", []) if c["name"] == "script" and not c["ok"]),
        "mirror_fail": sum(1 for r in recs for c in r.get("checks", []) if c["name"] == "lang_mirror" and not c["ok"]),
        "first_word_p50": percentile(fw, 50), "first_word_p95": percentile(fw, 95),
        "first_sound_p50": percentile(fs, 50), "first_sound_p95": percentile(fs, 95),
        "over_6s": _pct(sum(1 for x in fw if x > 6000), len(fw)),
        "barge_stop": percentile(barge, 50) if barge else None,
        "wer": round(100 * sum(wers) / len(wers), 1) if wers else None, "wer_n": len(wers),
        "wer_hinglish": round(100 * sum(wers_hi) / len(wers_hi), 1) if wers_hi else None, "wer_hinglish_n": len(wers_hi),
        "errors": sum(1 for r in recs if r.get("error")),
    }
    return m


def _fmt(v) -> str:
    if v is None:
        return "n/a"
    return f"{v:.0f}" if isinstance(v, float) and v > 100 else str(v)


def scorecard(summaries: dict[str, dict]) -> str:
    cfgs = list(summaries)
    lines = ["| Metric | Target | " + " | ".join(cfgs) + " |", "|---|---|" + "---|" * len(cfgs)]
    lines.append("| Scenarios passing | all | " + " | ".join(f"{s['scenarios_ok']}/{s['scenarios']}" for s in summaries.values()) + " |")
    lines.append("| Turns passing | all | " + " | ".join(f"{s['turns_ok']}/{s['turns']}" for s in summaries.values()) + " |")
    for key, (label, target, test) in TARGETS.items():
        cells = []
        for s in summaries.values():
            v = s.get(key)
            cells.append("n/a" if v is None else (_fmt(v) if target == "-" else f"{_fmt(v)} {'PASS' if test(v) else 'FAIL'}"))
        if all(c == "n/a" for c in cells):
            continue
        lines.append(f"| {label} | {target} | " + " | ".join(cells) + " |")
    lines.append("| Hinglish not mirrored (informational) | - | " + " | ".join(str(s["mirror_fail"]) for s in summaries.values()) + " |")
    lines.append("| Crashed turns (errors) | 0 | " + " | ".join(str(s["errors"]) for s in summaries.values()) + " |")
    return "\n".join(lines)


def failures(recs: list[dict], limit: int = 60) -> str:
    for r in recs:
        r["ok"] = not blocking(r["checks"])
    bad = [r for r in recs if not r["ok"]]
    out = []
    for r in bad[:limit]:
        why = "; ".join(f"{c['name']}: {c['detail']}" for c in blocking(r["checks"]))[:300]
        flag = " **WRONG ACTION**" if r.get("wrong_action") else ""
        out.append(f"- `{r['scenario']}` #{r['turn']}{flag}: said {r['say']!r} -> {(r.get('reply') or '')[:140]!r} [{r.get('intent')}] — {why}")
    if len(bad) > limit:
        out.append(f"- ... and {len(bad) - limit} more (see the JSONL)")
    return "\n".join(out) or "- none"


def by_tag(recs: list[dict]) -> str:
    groups: dict[str, list[bool]] = defaultdict(list)
    for r in recs:
        for t in r.get("tags", []):
            if t not in ("en", "hinglish", "hindi", "ledger", "session"):
                groups[t].append(r["ok"])
    rows = ["| Tag | Turns passing |", "|---|---|"]
    for t, v in sorted(groups.items()):
        rows.append(f"| {t} | {sum(v)}/{len(v)} ({100 * sum(v) // len(v)}%) |")
    return "\n".join(rows)


def load_run(path: Path) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for line in (path / "turns.jsonl").read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        out.setdefault(r.get("config", "run"), []).append(r)
    return out


def write_run(out_dir: Path, all_recs: dict[str, list[dict]], judge: dict | None, meta: dict) -> str:
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "turns.jsonl", "w", encoding="utf-8") as f:
        for recs in all_recs.values():
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    sums = {cfg: summarise(recs) for cfg, recs in all_recs.items()}
    md = [f"# Voice eval run {meta['stamp']}", "", f"Mode: **{meta['mode']}** | scenarios: {meta['scenario_count']} | files: {', '.join(meta['files'])}",
          f"Configs: {', '.join(sums)}" + (f" | noise: {meta['noise']}" if meta.get("noise") else ""), "", *([f"**WARNING:** {w}" for w in meta.get("warnings", [])] + [""] if meta.get("warnings") else []), "## Scorecard", "", scorecard(sums), ""]
    for cfg, recs in all_recs.items():
        md += [f"## {cfg}: pass rate by tag", "", by_tag(recs), "", f"## {cfg}: failing turns", "", failures(recs), ""]
    if judge:
        md += ["## Naturalness (LLM judge, small sample)", "", judge["markdown"], ""]
    text = "\n".join(md)
    (out_dir / "report.md").write_text(text, encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(sums, indent=1, default=str), encoding="utf-8")
    return text
