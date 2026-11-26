"""The throwaway backend as the test 'world': reset it between scenarios, read what a turn changed (sets logged, exercises skipped).
Only ever point this at a THROWAWAY backend (voice_agent/throwaway_backend.py, port 8011). `reset()` deletes today's sets."""
from __future__ import annotations

import os

import httpx

TOKEN = os.environ.get("EVAL_TOKEN", "testtoken")


class World:
    def __init__(self, url: str, token: str | None = None, db: str | None = None):
        self.url = url.rstrip("/")
        self.db = db or os.environ.get("EVAL_DB")      # throwaway database file: only used to undo skips on OLD backends that have no /live/unskip
        self.h = httpx.Client(base_url=self.url, headers={"Authorization": f"Bearer {token or TOKEN}"}, timeout=40.0)
        if "8001" in self.url or "8100" in self.url:
            raise SystemExit(f"refusing to run against {self.url}: that looks like the REAL backend. Use the throwaway backend (port 8011).")

    def health(self) -> bool:
        try:
            return self.h.get("/health").status_code == 200
        except httpx.HTTPError:
            return False

    def sets(self) -> list[dict]:
        return self.h.get("/session/sets").json()

    def state(self) -> dict:
        return self.h.get("/live/state").json() or {}

    def skipped(self) -> list[str]:
        return [e["name"] for e in self.state().get("exercises", []) if e.get("skipped")]

    def _plan_row(self):
        if not self.db:
            return None
        import sqlite3
        with sqlite3.connect(self.db) as c:
            return c.execute("SELECT * FROM plan ORDER BY rowid DESC LIMIT 1").fetchone()

    def reset(self, start: bool = True) -> dict:
        """Today's sets deleted, every skip undone, the plan put back if a scenario changed it (a swap), a fresh /live/start (clears any open question). Returns the state."""
        if self.db:
            import sqlite3
            if getattr(self, "_plan0", None) is None:
                with sqlite3.connect(self.db) as c:
                    cols = [r[1] for r in c.execute("PRAGMA table_info(plan)")]
                    self._plan0 = (cols, self._plan_row())
            else:
                cols, row = self._plan0
                now = self._plan_row()
                if row is not None and (now is None or now[1] != row[1]):
                    self.plan_changes = getattr(self, "plan_changes", 0) + 1
                    with sqlite3.connect(self.db) as c:
                        c.execute("DELETE FROM plan")
                        c.execute(f"INSERT INTO plan ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", row)
        for s in self.sets():
            self.h.delete(f"/session/set/{s['id']}")
        r = self.h.post("/live/unskip", json={"everything": True})
        if r.status_code == 404 and self.db:             # an old backend (before the un-skip endpoint): clear today's skip rows directly in the throwaway DB
            import sqlite3
            with sqlite3.connect(self.db) as c:
                c.execute("DELETE FROM memory WHERE kind = 'skip'")
        if start:
            self.h.post("/live/start")
        return self.state()

    def snapshot(self) -> dict:
        st = self.state()
        return {"sets": self.sets(), "skipped": [e["name"] for e in st.get("exercises", []) if e.get("skipped")], "state": st}
