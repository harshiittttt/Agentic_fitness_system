"""Push notifications: token store, user preferences, guarded sender (Expo push service) and the hook the Agent OS calls.

Everything here is opt-in and conservative:
  * OFF until the athlete turns it on (settings `push_prefs.enabled`), and only categories they left on are sent.
  * Quiet hours (comms.in_quiet_hours), a daily cap and a dedupe window are enforced in CODE. Only category `safety` bypasses
    quiet hours / cap / dedupe (an escalation to a professional should not wait for morning); it still needs a token + opt-in.
  * The trust dial is respected: `hands_on` only pushes things that need the athlete (approval, safety); `balanced`/`autopilot`
    follow the per-category switches. The kill switch silences everything except safety.
  * One user, one device token (the latest registered wins). DeviceNotRegistered (ticket or receipt) prunes the token.
  * No health detail goes into a push: callers pass a short text and the lock screen shows only that.
  * The network is `_post()`; tests monkeypatch it, nothing here is hit in tests.

Hook for the Agent OS: `push.notify(category, title, body, data=None)` (never raises). executor.py calls it after a nudge is stored.
Receipts: `push.check_receipts()` should be called now and then (scheduler/loop, or POST /push/receipts/check).
"""
from __future__ import annotations

import hashlib
import re

from . import clock, comms, ledger

EXPO_SEND = "https://exp.host/--/api/v2/push/send"
EXPO_RECEIPTS = "https://exp.host/--/api/v2/push/getReceipts"
BATCH = 100
DAILY_CAP = 3                    # pushes per rolling 24 h (safety and the test button are exempt)
TOKEN_RE = re.compile(r"^(Exponent|Expo)PushToken\[[A-Za-z0-9_\-]{6,}\]$")
CATEGORIES = {
    "nudge": "Coach messages and check-ins",
    "approval": "A change that needs your OK",
    "report": "Weekly review ready",
    "safety": "Safety: pain or a referral to a professional",
}
DEFAULT_CATS = {"nudge": True, "approval": True, "report": False, "safety": True}
HANDS_ON_ONLY = {"approval", "safety"}
# where a tap lands in the app (mobile/App.tsx reads data.screen)
SCREENS = {"nudge": "today", "approval": "decisions", "report": "insights", "safety": "today"}


def _post(url: str, payload, timeout: float = 15.0) -> dict:
    import httpx
    r = httpx.post(url, json=payload, timeout=timeout, headers={"Accept": "application/json", "Content-Type": "application/json"})
    r.raise_for_status()
    return r.json()


# ---- token + preferences ----
def valid_token(t: str) -> bool:
    return bool(isinstance(t, str) and TOKEN_RE.match(t.strip()))


def register(token: str, platform: str = "") -> dict:
    token = (token or "").strip()
    if not valid_token(token):
        raise ValueError("Not a valid Expo push token")
    rec = {"token": token, "platform": str(platform)[:20], "ts": clock.iso()}
    ledger.set_setting("push_token", rec)
    return rec


def unregister() -> bool:
    had = ledger.get_setting("push_token") is not None
    ledger.set_setting("push_token", None)
    return had


def token() -> str | None:
    rec = ledger.get_setting("push_token")
    return rec["token"] if rec else None


def prefs() -> dict:
    p = ledger.get_setting("push_prefs") or {}
    cats = {**DEFAULT_CATS, **{k: bool(v) for k, v in (p.get("categories") or {}).items() if k in CATEGORIES}}
    return {"enabled": bool(p.get("enabled", False)), "categories": cats}


def set_prefs(enabled: bool | None = None, categories: dict | None = None) -> dict:
    cur = prefs()
    if enabled is not None:
        cur["enabled"] = bool(enabled)
    for k, v in (categories or {}).items():
        if k not in CATEGORIES:
            raise ValueError(f"Unknown category {k}")
        cur["categories"][k] = bool(v)
    ledger.set_setting("push_prefs", cur)
    return cur


def status() -> dict:
    return {**prefs(), "registered": token() is not None, "category_labels": CATEGORIES,
            "quiet_hours": {"start": comms.QUIET_START, "end": comms.QUIET_END, "now": comms.in_quiet_hours()},
            "daily_cap": DAILY_CAP, "sent_last_24h": len(_recent_log())}


# ---- send log (cap + dedupe) ----
def _recent_log() -> list[dict]:
    return [e for e in (ledger.get_setting("push_log") or []) if clock.age_days(e["ts"]) * 24 <= 24]


def _remember(category: str, fp: str) -> None:
    log = _recent_log() + [{"ts": clock.iso(), "category": category, "fp": fp}]
    ledger.set_setting("push_log", log[-50:])


def _fp(category: str, title: str, body: str) -> str:
    return hashlib.sha1(f"{category}|{title}|{body}".encode()).hexdigest()[:16]


# ---- sender ----
def _chunks(xs: list, n: int):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def send_raw(messages: list[dict]) -> list[dict]:
    """Batch to Expo, return the tickets (one per message). Prunes the token on DeviceNotRegistered. Network errors -> error tickets."""
    tickets: list[dict] = []
    for chunk in _chunks(messages, BATCH):
        try:
            res = _post(EXPO_SEND, chunk)
            data = res.get("data") if isinstance(res, dict) else None
            if not isinstance(data, list) or len(data) != len(chunk):
                data = [{"status": "error", "message": "unexpected response"}] * len(chunk)
        except Exception as e:  # network down, 4xx/5xx: never crash the control loop
            data = [{"status": "error", "message": type(e).__name__}] * len(chunk)
        tickets.extend(data)
    pending = ledger.get_setting("push_tickets") or []
    for m, t in zip(messages, tickets):
        if t.get("status") == "ok" and t.get("id"):
            pending.append({"id": t["id"], "token": m["to"], "ts": clock.iso()})
        elif (t.get("details") or {}).get("error") == "DeviceNotRegistered" and token() == m["to"]:
            unregister()
    ledger.set_setting("push_tickets", pending[-200:])
    return tickets


def check_receipts() -> dict:
    """Ask Expo how earlier pushes went (>= 15 min after sending). Prunes a token the OS says is gone."""
    pending = ledger.get_setting("push_tickets") or []
    ready = [p for p in pending if clock.age_days(p["ts"]) * 1440 >= 15]
    if not ready:
        return {"checked": 0, "ok": 0, "errors": 0, "pruned": False}
    ok = errors = 0
    pruned = False
    done: set[str] = set()
    for chunk in _chunks(ready, 300):
        try:
            res = _post(EXPO_RECEIPTS, {"ids": [p["id"] for p in chunk]})
            data = (res or {}).get("data") or {}
        except Exception:
            continue
        for p in chunk:
            r = data.get(p["id"])
            if not r:
                continue
            done.add(p["id"])
            if r.get("status") == "ok":
                ok += 1
            else:
                errors += 1
                if (r.get("details") or {}).get("error") == "DeviceNotRegistered" and token() == p["token"]:
                    unregister()
                    pruned = True
    ledger.set_setting("push_tickets", [p for p in pending if p["id"] not in done and clock.age_days(p["ts"]) <= 1])
    return {"checked": len(done), "ok": ok, "errors": errors, "pruned": pruned}


def _decide(category: str) -> tuple[bool, str]:
    """-> (may send, reason if not). Pure policy, no network."""
    if category not in CATEGORIES:
        return False, "unknown_category"
    if not token():
        return False, "no_token"
    p = prefs()
    if not p["enabled"]:
        return False, "opted_out"
    if category == "safety":
        return True, ""
    if not p["categories"].get(category):
        return False, "category_off"
    if ledger.kill_switch():
        return False, "kill_switch"
    if ledger.dial() == "hands_on" and category not in HANDS_ON_ONLY:
        return False, "dial_hands_on"
    if comms.in_quiet_hours():
        return False, "quiet_hours"
    if len(_recent_log()) >= DAILY_CAP:
        return False, "daily_cap"
    return True, ""


def _err(t: dict) -> str:
    return (t.get("details") or {}).get("error") or t.get("message", "")


def notify(category: str, title: str, body: str, data: dict | None = None) -> dict:
    """The Agent OS hook. Never raises. -> {sent, reason}."""
    try:
        title, body = comms.clean(title)[:60], comms.clean(body)
        ok, why = _decide(category)
        if not ok:
            return {"sent": False, "reason": why}
        fp = _fp(category, title, body)
        if category != "safety" and any(e["fp"] == fp for e in _recent_log()):
            return {"sent": False, "reason": "duplicate"}
        msg = {"to": token(), "title": title, "body": body, "sound": "default", "channelId": "default",
               "priority": "high" if category == "safety" else "default",
               "data": {"screen": SCREENS[category], "category": category, **(data or {})}}
        t = send_raw([msg])[0]
        if t.get("status") != "ok":
            return {"sent": False, "reason": "send_failed", "error": _err(t)}
        _remember(category, fp)
        return {"sent": True, "reason": ""}
    except Exception as e:
        return {"sent": False, "reason": "error", "error": type(e).__name__}


def send_test() -> dict:
    """The Settings test button: needs only a token (skips opt-in, quiet hours and cap so the athlete can check it works)."""
    tok = token()
    if not tok:
        return {"sent": False, "reason": "no_token"}
    t = send_raw([{"to": tok, "title": "Test notification", "body": "Push is working. Tap to open the app.", "sound": "default",
                   "channelId": "default", "data": {"screen": "today", "category": "test"}}])[0]
    ok = t.get("status") == "ok"
    return {"sent": ok, "reason": "" if ok else "send_failed", "error": "" if ok else _err(t)}


def notify_nudge(text: str, nudge_id: int | None = None) -> dict:
    """Called when an applied nudge lands in the nudge table."""
    return notify("nudge", "Your coach", text, {"nudge_id": nudge_id} if nudge_id else None)


def notify_pending(summary: str, decision_id: int | None = None) -> dict:
    """Call when a decision is waiting for the athlete's OK."""
    return notify("approval", "Needs your OK", summary, {"decision_id": decision_id} if decision_id else None)


PENDING_MAX_AGE_DAYS = 2         # older waiting decisions are not pushed (no burst after a late opt-in)
PENDING_PER_TICK = 1             # rate limit: at most one approval push per scheduler tick


def notify_pending_decisions() -> dict:
    """Scheduler hook: push ONE 'needs your OK' for decisions waiting in `pending_approval`, each decision at most once.

    Never raises, never touches the network without a token + opt-in (policy in notify/_decide). A decision is marked as
    notified only once the push was sent. Quiet hours / daily cap / failures leave it unmarked so a later tick retries
    (until PENDING_MAX_AGE_DAYS). Detail stays out of the push text. -> {sent, considered, skipped?}."""
    try:
        if not token() or not prefs()["enabled"]:
            return {"sent": 0, "considered": 0, "skipped": "no_token_or_opted_out"}
        done = set(ledger.get_setting("push_pending_notified") or [])
        todo = [d for d in reversed(ledger.recent(20, "pending_approval"))
                if d["id"] not in done and clock.age_days(d["ts"]) <= PENDING_MAX_AGE_DAYS]
        sent = 0
        last = ""
        for d in todo[:PENDING_PER_TICK]:
            r = notify_pending(f"A coach change (#{d['id']}) is waiting for your OK.", d["id"])
            if r.get("sent"):
                sent += 1
                done.add(d["id"])
            else:
                last = r.get("reason", "")
        if sent:
            ledger.set_setting("push_pending_notified", sorted(done)[-200:])
        return {"sent": sent, "considered": len(todo), **({"skipped": last} if last else {})}
    except Exception as e:
        return {"sent": 0, "considered": 0, "error": type(e).__name__}


def notify_safety(text: str) -> dict:
    return notify("safety", "Safety check", text)
