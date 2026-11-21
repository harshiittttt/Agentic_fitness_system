"""SQLite storage + long-term memory notes."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

def _default_db_path() -> str:
    """FITNESS_DB (explicit file) > FITNESS_DATA_DIR/fitness.db > repo-root fitness.db (local dev). Everything else that
    persists (tts_cache/, traces, memory, push, wearables, credit counters) lives in this DB or beside it, so one
    FITNESS_DATA_DIR relocates all state (one isolated instance per person)."""
    explicit = os.environ.get("FITNESS_DB")
    if explicit:
        return explicit
    data_dir = (os.environ.get("FITNESS_DATA_DIR") or "").strip()
    if data_dir:
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, "fitness.db")
    return os.path.join(os.path.dirname(__file__), "..", "..", "fitness.db")


DB_PATH = _default_db_path()

SCHEMA = """
CREATE TABLE IF NOT EXISTS profile (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, session TEXT NOT NULL,
  exercise TEXT NOT NULL, weight REAL, reps INTEGER, rpe REAL, note TEXT
);
CREATE TABLE IF NOT EXISTS checkins (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, sleep_h REAL, soreness INTEGER, energy INTEGER,
  bodyweight REAL, note TEXT
);
CREATE TABLE IF NOT EXISTS meals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, day TEXT NOT NULL, name TEXT NOT NULL, grams REAL,
  kcal REAL, protein REAL, carbs REAL, fat REAL, estimated INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tts_usage (
  month TEXT PRIMARY KEY, credits REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS memory (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plan (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  data TEXT NOT NULL, updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS change_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, agent TEXT NOT NULL, summary TEXT NOT NULL,
  reason TEXT, evidence TEXT, status TEXT NOT NULL DEFAULT 'applied'
);
"""


def now() -> str:
    override = os.environ.get("FITNESS_NOW")  # simulated clock for tests/demos (see fitness_os/clock.py)
    if override:
        d = datetime.fromisoformat(override)
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).isoformat(timespec="seconds")
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        c.executescript(SCHEMA)
        yield c
        c.commit()
    finally:
        c.close()


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


# --- profile / plan (single JSON docs) ---
def save_profile(data: dict) -> None:
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO profile (id, data) VALUES (1, ?)", (json.dumps(data),))


def get_profile() -> dict | None:
    with conn() as c:
        r = c.execute("SELECT data FROM profile WHERE id = 1").fetchone()
        return json.loads(r["data"]) if r else None


def save_plan(data: dict) -> None:
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO plan (id, data, updated) VALUES (1, ?, ?)", (json.dumps(data), now()))


def get_plan() -> dict | None:
    with conn() as c:
        r = c.execute("SELECT data FROM plan WHERE id = 1").fetchone()
        return json.loads(r["data"]) if r else None


# --- sets ---
def add_set(session: str, exercise: str, weight: float | None, reps: int | None,
            rpe: float | None = None, note: str | None = None) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO sets (ts, session, exercise, weight, reps, rpe, note) VALUES (?,?,?,?,?,?,?)",
            (now(), session, exercise.lower(), weight, reps, rpe, note),
        )
        return cur.lastrowid


def get_sets(exercise: str | None = None, limit: int = 200) -> list[dict]:
    with conn() as c:
        if exercise:
            cur = c.execute("SELECT * FROM sets WHERE exercise = ? ORDER BY id DESC LIMIT ?", (exercise.lower(), limit))
        else:
            cur = c.execute("SELECT * FROM sets ORDER BY id DESC LIMIT ?", (limit,))
        return _rows(cur)


def session_names() -> list[str]:
    """Distinct session labels, oldest first."""
    with conn() as c:
        rows = c.execute("SELECT session FROM sets GROUP BY session ORDER BY MIN(id)").fetchall()
        return [r["session"] for r in rows]


def reset_history() -> None:
    """Clear logged sets, check-ins and the change log (keeps profile/plan). Used by demo mode."""
    with conn() as c:
        for t in ("sets", "checkins", "change_log", "meals"):
            c.execute(f"DELETE FROM {t}")


def reset_all() -> None:
    """Wipe everything (profile, plan, history, memory) so the app starts from onboarding."""
    with conn() as c:
        for t in ("sets", "checkins", "change_log", "meals", "memory", "plan", "profile"):
            c.execute(f"DELETE FROM {t}")


# --- check-ins ---
def add_checkin(sleep_h=None, soreness=None, energy=None, bodyweight=None, note=None) -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO checkins (ts, sleep_h, soreness, energy, bodyweight, note) VALUES (?,?,?,?,?,?)",
            (now(), sleep_h, soreness, energy, bodyweight, note),
        )


def get_checkins(limit: int = 30) -> list[dict]:
    with conn() as c:
        return _rows(c.execute("SELECT * FROM checkins ORDER BY id DESC LIMIT ?", (limit,)))


# --- meals ---
def add_meal(day: str, name: str, grams, kcal, protein, carbs, fat, estimated: bool = False) -> int:
    with conn() as c:
        cur = c.execute(
            "INSERT INTO meals (ts, day, name, grams, kcal, protein, carbs, fat, estimated) VALUES (?,?,?,?,?,?,?,?,?)",
            (now(), day, name, grams, kcal, protein, carbs, fat, int(estimated)),
        )
        return cur.lastrowid


def get_meals(day: str) -> list[dict]:
    with conn() as c:
        return _rows(c.execute("SELECT * FROM meals WHERE day = ? ORDER BY id", (day,)))


def get_meal(meal_id: int) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM meals WHERE id = ?", (meal_id,)).fetchone()
        return dict(r) if r else None


def recent_meals(limit: int = 10) -> list[dict]:
    """Foods you log most often (same name and portion), most frequent first, newest as tie-break."""
    with conn() as c:
        return _rows(c.execute(
            "SELECT name, ROUND(grams) AS grams, kcal, protein, carbs, fat, estimated, MAX(id) AS id, COUNT(*) AS n "
            "FROM meals GROUP BY name, ROUND(grams) ORDER BY n DESC, id DESC LIMIT ?", (limit,)))


def delete_meal(meal_id: int) -> None:
    with conn() as c:
        c.execute("DELETE FROM meals WHERE id = ?", (meal_id,))


# --- ElevenLabs credit usage (account-level, so reset_all leaves it alone) ---
def tts_usage(month: str) -> float:
    with conn() as c:
        r = c.execute("SELECT credits FROM tts_usage WHERE month = ?", (month,)).fetchone()
        return float(r["credits"]) if r else 0.0


def add_tts_usage(month: str, credits: float) -> None:
    with conn() as c:
        c.execute("INSERT INTO tts_usage (month, credits) VALUES (?, ?) ON CONFLICT(month) DO UPDATE SET credits = credits + ?",
                  (month, credits, credits))


# --- long-term memory ---
def add_memory(kind: str, text: str) -> None:
    with conn() as c:
        c.execute("INSERT INTO memory (ts, kind, text) VALUES (?,?,?)", (now(), kind, text))


def get_memory(limit: int = 50) -> list[dict]:
    with conn() as c:
        return _rows(c.execute("SELECT * FROM memory ORDER BY id DESC LIMIT ?", (limit,)))


# --- change log ---
def log_change(agent: str, summary: str, reason: str = "", evidence: str = "", status: str = "applied") -> None:
    with conn() as c:
        c.execute(
            "INSERT INTO change_log (ts, agent, summary, reason, evidence, status) VALUES (?,?,?,?,?,?)",
            (now(), agent, summary, reason, evidence, status),
        )


def get_changes(limit: int = 50) -> list[dict]:
    with conn() as c:
        return _rows(c.execute("SELECT * FROM change_log ORDER BY id DESC LIMIT ?", (limit,)))


def age_days(ts: str) -> float:
    return (datetime.fromisoformat(now()) - datetime.fromisoformat(ts)).total_seconds() / 86400


def recent_change_count(days: float = 14) -> int:
    """Agent-driven plan changes in the last `days` days (for soft dampening). Onboarding and manual
    swaps don't count: they say nothing about how unstable the evidence-driven plan is."""
    return sum(1 for r in get_changes(100)
               if r["status"] == "applied" and r["agent"] in ("training", "nutrition", "recovery") and age_days(r["ts"]) <= days)


def days_since_last_change(agents: tuple = ("training", "recovery")) -> float:
    """Days since an applied change by any of `agents`; infinity if none."""
    for r in get_changes(100):
        if r["status"] == "applied" and r["agent"] in agents:
            return age_days(r["ts"])
    return float("inf")
