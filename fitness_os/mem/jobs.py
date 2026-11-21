"""The memory layer's background work, run by the scheduler (fitness_os/scheduler.py: jobs `memory.nightly` 03:30 and `memory.weekly` Sunday 04:00).

Every step is optional and isolated: a module that is not built yet is skipped, a step that raises is recorded and the rest still run. Nothing here
needs an LLM key; steps that can use a model degrade to deterministic output without one.
"""
from __future__ import annotations

import importlib

NIGHTLY = [("forecast", "resolve_due"), ("consolidate", "run"), ("embed", "reindex"), ("miner", "run"), ("dossier", "refresh"), ("questions", "generate")]
WEEKLY = [("miner", "run_full"), ("dossier", "refresh"), ("reveal", "weekly")]


def _run(steps: list[tuple[str, str]]) -> dict:
    report: dict = {}
    for module, fn in steps:
        name = f"{module}.{fn}"
        try:
            mod = importlib.import_module(f"fitness_os.mem.{module}")
        except ModuleNotFoundError as e:
            if e.name == f"fitness_os.mem.{module}":
                report[name] = {"skipped": "not built"}
                continue
            report[name] = {"error": f"import failed: {e}"[:160]}
            continue
        f = getattr(mod, fn, None)
        if f is None:
            report[name] = {"skipped": f"no {fn}()"}
            continue
        try:
            res = f()
            report[name] = res if isinstance(res, dict) else {"result": res if isinstance(res, (int, float, str, bool, type(None))) else len(res)}
        except Exception as e:      # one failing step never stops the others, and never reaches the athlete
            report[name] = {"error": f"{type(e).__name__}: {e}"[:160]}
    return report


def nightly() -> dict:
    return _run(NIGHTLY)


def weekly() -> dict:
    return _run(WEEKLY)
