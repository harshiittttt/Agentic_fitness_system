"""HTTP face of the athlete model, mounted by backend/main.py at /memory. This is what the "What I know about you" screen talks to.

Everything the athlete sees can be confirmed, corrected or forgotten here, and it sticks: a denied belief is never resurrected by anything but
the athlete themselves. Health-sensitive items are flagged (`sensitive`) so the screen can collapse them.
"""
from __future__ import annotations

import importlib
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import clock
from . import ingest, profile_sync, store

# not `from . import dossier`: the package facade (mem/__init__.py) has functions with these names, which would win
dossier, questions, reveal = (importlib.import_module(f"{__package__}.{m}") for m in ("dossier", "questions", "reveal"))

router = APIRouter(prefix="/memory", tags=["athlete-model"])


class CorrectIn(BaseModel):
    value: str


class AnswerIn(BaseModel):
    answer: str


class RememberIn(BaseModel):
    text: str


class SearchIn(BaseModel):
    query: str
    k: int = 8


def _refresh_quiet() -> dict | None:
    try:
        return dossier.build(True)
    except Exception:
        return None


def _fact_or_404(fact_id: int) -> dict:
    f = store.get_fact(fact_id)
    if not f:
        raise HTTPException(404, "No such belief")
    return f


def _item(f: dict) -> dict:
    f = dict(f)
    f["conf"] = round(store.confidence(f), 4)
    return {**dossier._fact_item(f), "status": f["status"], "kind": f["kind"]}


def _after(fact: dict | None = None) -> dict:
    d = _refresh_quiet()
    return {"ok": True, "fact": _item(fact) if fact and fact["status"] in ("active", "disputed") else None, "coverage": (d or {}).get("coverage"), "version": (d or {}).get("version")}


# ---------------------------------------------------------------- reads
@router.get("/overview")
def overview():
    d = dossier.build(False)
    disc = [x for x in store.discoveries(status=("confirmed", "weakening"), limit=50) if x["user_state"] == "none"]
    rv = reveal.latest()
    known = sum(1 for s in d["slots"] if s["known"])
    return {"coverage": d["coverage"], "coverage_pct": round(d["coverage"] * 100), "slots_known": known, "slots_total": len(d["slots"]), "slots": d["slots"], "missing": d["missing"],
            "narrative": d["narrative"], "version": d["version"], "changed": (d.get("diff") or {}).get("summary", ""), "counts": d["counts"],
            "stage": "new" if d["coverage"] < 0.4 and not d["counts"]["discoveries"] else "knowing", "pending_discoveries": len(disc), "open_questions": len(questions.peek(10)),
            "reveal": {"ts": rv["ts"], "cards": len(rv["cards"]), "age_days": rv["age_days"]}, "stats": store.stats()}


@router.get("/dossier")
def get_dossier(refresh: bool = False):
    return dossier.build(refresh)


@router.get("/facts")
def get_facts(status: str = "active", sensitivity: str | None = None, q: str = "", limit: int = 100):
    st = {"active": ("active",), "all": ("active", "disputed", "superseded"), "disputed": ("disputed",), "superseded": ("superseded",)}.get(status, ("active",))
    rows = store.facts(status=st, sensitivity=sensitivity, limit=max(1, min(limit, 300)))
    rows = [r for r in rows if r["predicate"] not in dossier.HIDE_PREDS and r["user_state"] != "denied"]
    if q.strip():
        qn = store.norm(q)
        rows = [r for r in rows if qn in store.norm(r["body"])]
    return {"facts": [_item(r) for r in rows]}


@router.get("/discoveries")
def get_discoveries(all: bool = False):
    out = []
    for d in store.discoveries(status=("confirmed", "weakening"), limit=60):
        if d["user_state"] == "denied":
            continue
        if not all and d["user_state"] not in ("none", "confirmed"):
            continue
        it = dossier._discovery_item(d)
        out.append({**it, "id": d["id"], "status": d["status"], "pending": d["user_state"] == "none", "chips": [e["text"] for e in it["evidence"]]})
    return {"discoveries": out}


@router.get("/questions")
def get_questions(limit: int = 1, exclude: str = ""):
    qs = questions.next_questions(max(1, min(limit, 3)), tuple(x for x in exclude.split(",") if x))
    return {"questions": qs, "queued": len(questions.peek(20))}


@router.get("/reveal")
def get_reveal(refresh: bool = False):
    return reveal.weekly() if refresh else reveal.latest()


def _timeline(limit: int) -> list[dict]:
    items: list[dict] = []
    for a in store.audit_log(250):
        act, ref = a["action"], a["ref"]
        if not ref.isdigit() and act.startswith("fact"):
            continue
        f = store.get_fact(int(ref)) if ref.isdigit() and act.startswith("fact") else None
        sens = bool(f and f["sensitivity"] != "normal")
        ts, text = a["ts"], None
        if act == "fact.add" and f and f["user_state"] != "denied" and f["predicate"] not in dossier.HIDE_PREDS and f["status"] in ("active", "superseded"):
            text = "I learned: " + dossier.phrase(f)
        elif act == "fact.confirm" and f:
            text = "You confirmed: " + dossier.phrase(f)
        elif act == "fact.correct":
            m = re.search(r"-> (\d+)", a["detail"] or "")
            nf = store.get_fact(int(m.group(1))) if m else None
            sens = bool(nf and nf["sensitivity"] != "normal")
            text = "You corrected me: " + (dossier.phrase(nf) if nf else "updated") if nf else None
        elif act == "fact.deny":
            text = "You asked me to forget something"
        elif act in ("fact.ended", "fact.supersede") and f and f["user_state"] != "denied":
            text = "No longer true: " + dossier.phrase(f)
        elif act == "reveal.weekly":
            text = "Weekly summary ready: " + (a["detail"] or "")
        if text:
            items.append({"ts": ts, "kind": act.split(".")[0] if act.startswith("fact") else act.split(".")[0], "action": act, "text": text, "fact_id": f["id"] if f else None, "sensitive": sens})
    for f in store.facts(status=("superseded",), limit=60, order="recent"):     # replaced by a newer value (automatic supersession is not audited)
        if f["user_state"] != "denied" and f["valid_to"] and f["predicate"] not in dossier.HIDE_PREDS:
            items.append({"ts": f["valid_to"], "kind": "fact", "action": "fact.ended", "text": "No longer true: " + dossier.phrase(f), "fact_id": f["id"], "sensitive": f["sensitivity"] != "normal"})
    for d in store.discoveries(status=("confirmed", "weakening"), limit=40):
        if d["user_state"] != "denied":
            items.append({"ts": d["first_seen"], "kind": "discovery", "action": "discovery.found", "text": "I found a pattern in your data: " + d["statement"], "discovery_id": d["id"], "sensitive": False})
    for v in store.dossier_history(12):
        s = (v.get("diff") or {}).get("summary")
        if s:
            items.append({"ts": v["ts"], "kind": "dossier", "action": "dossier.version", "text": f"What I know about you was updated: {s}", "version": v["version"], "sensitive": False})
    items.sort(key=lambda i: i["ts"], reverse=True)
    seen, out = set(), []
    for i in items:
        k = (i["text"], i["ts"][:16])
        if k not in seen:
            seen.add(k)
            out.append(i)
    return out[:limit]


@router.get("/timeline")
def get_timeline(limit: int = 40):
    return {"events": _timeline(max(1, min(limit, 200)))}


@router.post("/search")
def search(body: SearchIn):
    from . import NotBuilt, search as _search
    try:
        hits = _search(body.query[:200], max(1, min(body.k, 20)))
    except NotBuilt:
        hits = []
    out = []
    for h in hits:
        d = h.as_dict()
        if h.item_type == "fact":
            f = store.get_fact(h.item_id)
            if not f or f["user_state"] == "denied" or f["status"] not in ("active", "disputed"):
                continue
            d["text"] = dossier.phrase(f)
            d["sensitive"] = f["sensitivity"] != "normal"
        out.append(d)
    return {"hits": out}


# ---------------------------------------------------------------- writes
@router.post("/remember")
def remember(body: RememberIn):
    text = ingest.sanitize(body.text, 400)
    if not text:
        raise HTTPException(400, "Nothing to remember")
    if ingest.is_instruction(text):
        raise HTTPException(400, "I can only remember plain facts about you, not instructions.")
    learned = ingest.observe(text, source="remember", channel="app", explicit=True)
    d = _refresh_quiet()
    return {"learned": [{k: v for k, v in x.items() if k in ("id", "predicate", "object", "body", "created", "reinforced", "blocked", "sensitivity")} for x in learned],
            "understood": bool(learned), "coverage": (d or {}).get("coverage")}


@router.post("/facts/{fact_id}/confirm")
def confirm(fact_id: int):
    _fact_or_404(fact_id)
    return _after(store.user_confirm(fact_id))


@router.post("/facts/{fact_id}/deny")
def deny(fact_id: int):
    _fact_or_404(fact_id)
    store.user_deny(fact_id, "denied in the app")
    return _after(None)


@router.post("/facts/{fact_id}/forget")
def forget(fact_id: int):
    f = _fact_or_404(fact_id)
    store.user_deny(fact_id, "forgotten in the app")
    out = _after(None)
    if profile_sync.backed(f):   # forgetting never edits the profile; say so rather than leave the athlete guessing
        out["note"] = "Forgotten as a memory. Your profile, targets and plan are unchanged; correct the value instead to change them."
    return out


@router.post("/facts/{fact_id}/correct")
def correct(fact_id: int, body: CorrectIn):
    f = _fact_or_404(fact_id)
    value = ingest.safe_phrase(body.value)
    if not value or ingest.is_instruction(body.value):
        raise HTTPException(400, "Give the corrected value in a few plain words.")
    synced = None
    try:   # a belief the profile backs changes the profile, targets and plan first; if that is refused the belief stays as it was
        plan = profile_sync.plan_correction(f, value)
        if plan:
            patch, value = plan
            synced = profile_sync.apply(patch)
    except profile_sync.ProfileRefused as e:
        raise HTTPException(400, str(e))
    num = None
    m = re.search(r"\d+(?:\.\d+)?", value)
    if f["predicate"] in ("trains_days_per_week", "prefers_session_minutes", "target_weight_kg") and m:
        num, value = float(m.group()), m.group()
    tpl = ingest.BODY_TPL.get(f["predicate"])
    new = store.user_correct(fact_id, value, body=tpl.replace("{o}", value) if tpl else None, value_num=num)
    if not new:
        raise HTTPException(404, "No such belief")
    out = _after(new)
    if synced is not None:
        out["profile"] = {"changed": synced["changed"], "nutrition_updated": synced["nutrition_updated"], "plan_rebuilt": synced["rebuilt"]}
    return out


@router.post("/discoveries/{discovery_id}/confirm")
def discovery_confirm(discovery_id: int):
    if not store.get_discovery(discovery_id):
        raise HTTPException(404, "No such pattern")
    store.set_discovery(discovery_id, user_state="confirmed")
    store.audit("discovery.confirm", discovery_id)
    return {"ok": True}


@router.post("/discoveries/{discovery_id}/dismiss")
def discovery_dismiss(discovery_id: int):
    if not store.get_discovery(discovery_id):
        raise HTTPException(404, "No such pattern")
    store.set_discovery(discovery_id, user_state="denied")
    store.audit("discovery.deny", discovery_id)
    return {"ok": True}


@router.post("/questions/{question_id}/answer")
def answer_question(question_id: int, body: AnswerIn):
    r = questions.answer(question_id, body.answer)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "Could not use that answer")
    return {**r, "next": questions.peek(1)}


@router.post("/questions/{question_id}/dismiss")
def dismiss_question(question_id: int):
    return questions.dismiss(question_id)
