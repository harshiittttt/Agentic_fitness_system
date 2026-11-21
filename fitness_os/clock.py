"""Clock with an override so simulated days can pass in tests and demos (FITNESS_NOW=ISO timestamp)."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone


def now() -> datetime:
    raw = os.environ.get("FITNESS_NOW")
    return parse(raw) if raw else datetime.now(timezone.utc).replace(microsecond=0)


def parse(ts: str) -> datetime:
    d = datetime.fromisoformat(ts)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def iso() -> str:
    return now().isoformat(timespec="seconds")


def plus(**kw) -> str:
    return (now() + timedelta(**kw)).isoformat(timespec="seconds")


def age_days(ts: str) -> float:
    return (now() - parse(ts)).total_seconds() / 86400
