"""Background scheduler loop: calls `scheduler.run_due()` every minute while the backend is running.

Because jobs are 'due until the most recent scheduled moment has run', a laptop that was asleep at 05:30 simply catches up on the
next tick after it wakes, and a job never runs twice. Disable with FITNESS_SCHEDULER=0. A tick that raises is recorded and the loop
keeps going: one bad night must not stop tomorrow's run.
"""
from __future__ import annotations

import os
import threading
from typing import Callable

from . import clock

_thread: threading.Thread | None = None
_stop = threading.Event()
_state: dict = {"interval": None, "last_tick": None, "ticks": 0, "last_error": None}


def enabled() -> bool:
    return os.environ.get("FITNESS_SCHEDULER", "1") != "0"


def running() -> bool:
    return bool(_thread and _thread.is_alive())


def status() -> dict:
    return {"enabled": enabled(), "running": running(), **_state}


def start(interval: float = 60.0, first_delay: float | None = None, tick: Callable[[], object] | None = None) -> bool:
    """Start the loop once (idempotent). Returns False if it is disabled or already running."""
    global _thread
    if not enabled() or running():
        return False
    if tick is None:
        from . import scheduler
        tick = scheduler.run_due
    if first_delay is None:
        first_delay = float(os.environ.get("FITNESS_SCHEDULER_DELAY", "20"))   # let the server finish starting before the first tick
    _stop.clear()
    _state.update(interval=interval, last_tick=None, ticks=0, last_error=None)

    def loop() -> None:
        if _stop.wait(first_delay):
            return
        while not _stop.is_set():
            try:
                tick()
                _state["last_error"] = None
            except Exception as e:  # keep going: tomorrow's run must not depend on tonight's failure
                _state["last_error"] = f"{type(e).__name__}: {str(e)[:160]}"
            _state["ticks"] += 1
            _state["last_tick"] = clock.iso()
            if _stop.wait(interval):
                return

    _thread = threading.Thread(target=loop, name="fitness-scheduler", daemon=True)
    _thread.start()
    return True


def stop(timeout: float = 2.0) -> None:
    _stop.set()
    if _thread:
        _thread.join(timeout)
