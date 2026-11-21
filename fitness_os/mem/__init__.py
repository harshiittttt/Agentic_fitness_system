"""The athlete memory layer: the facade every agent, route and screen uses. See DESIGN.md.

    from fitness_os import mem
    mem.search("knee", k=6)              hybrid retrieval over beliefs + discoveries            (memory-core)
    mem.build_pack("training", "squat day")  a budgeted, relevant block of what is known            (memory-core)
    mem.observe("my left knee aches")    turn something the athlete said into beliefs           (athlete-model)
    mem.mine()                           find statistically valid personal patterns             (insight-engine)
    mem.predict("e1rm", "squat")         forecast + log, so surprises can be learned from       (insight-engine)
    mem.get_dossier()                    the living model of the athlete                        (athlete-model)
    mem.next_questions()                 what the system most wants to ask next                 (athlete-model)
    mem.remember(...)/mem.know(...)      write / read beliefs directly                          (store)

(Facade names never equal a submodule name: importing mem.pack, mem.dossier... must not replace a function.)
Each function imports its module lazily, so a module that is not built yet degrades to `NotBuilt` instead of breaking the app.
"""
from __future__ import annotations

import importlib
from typing import Any

from . import store                                   # noqa: F401  (re-exported: mem.store)
from .types import Hit, Pack, register_predicate      # noqa: F401


class NotBuilt(RuntimeError):
    pass


def _call(module: str, fn: str, *a, **kw) -> Any:
    try:
        mod = importlib.import_module(f"{__name__}.{module}")
    except ModuleNotFoundError as e:
        if e.name == f"{__name__}.{module}":
            raise NotBuilt(f"fitness_os.mem.{module} is not built yet") from e
        raise
    return getattr(mod, fn)(*a, **kw)


# ---- memory core ----
def search(query: str, k: int = 8, **kw) -> list[Hit]:
    return _call("retrieve", "search", query, k, **kw)


def build_pack(agent: str, query: str = "", budget_tokens: int = 900, live_context: str = "") -> Pack:
    return _call("pack", "build", agent, query, budget_tokens, live_context)


# ---- athlete model ----
def observe(text: str, source: str = "chat", channel: str = "chat", ts: str | None = None) -> list[dict]:
    return _call("ingest", "observe", text, source, channel, ts)


def get_dossier(refresh: bool = False) -> dict:
    return _call("dossier", "build", refresh)


def next_questions(limit: int = 3) -> list[dict]:
    return _call("questions", "next", limit)


# ---- insight engine ----
def mine(force: bool = False) -> dict:
    return _call("miner", "run", force)


def predict(target: str, subject: str | None = None, **kw) -> dict:
    return _call("forecast", "predict", target, subject, **kw)


# ---- direct belief access ----
def remember(pred: str, obj: str, **kw) -> dict:
    return store.add_fact(pred, obj, **kw)


def know(pred: str | None = None, **kw) -> list[dict]:
    return store.facts(predicate=pred, **kw)
