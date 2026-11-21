"""FITNESS_DATA_DIR relocates all writable state; two data dirs never share state (one isolated instance per person)."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

CODE = r"""
import sys
from backend.memory import db
from backend.coach import voice
db.save_profile({"name": sys.argv[1]})
print(db.DB_PATH); print(voice.cache_dir()); print(db.get_profile()["name"])
"""


def run(data_dir, name):
    env = {k: v for k, v in os.environ.items() if k not in ("FITNESS_DB", "FITNESS_DATA_DIR")}
    env["FITNESS_DATA_DIR"] = str(data_dir)
    env["PYTHONPATH"] = str(ROOT)
    out = subprocess.run([sys.executable, "-c", CODE, name], cwd=ROOT, env=env, capture_output=True, text=True, check=True)
    return out.stdout.split("\n")[:3]


def test_state_lands_under_data_dir_and_is_isolated(tmp_path):
    a, b = tmp_path / "alice", tmp_path / "bob"
    pa, ca, na = run(a, "alice")
    pb, cb, nb = run(b, "bob")
    assert Path(pa).resolve() == (a / "fitness.db").resolve() and (a / "fitness.db").exists()
    assert Path(ca).resolve() == (a / "tts_cache").resolve()
    assert Path(pb).resolve() == (b / "fitness.db").resolve() and Path(cb).resolve() == (b / "tts_cache").resolve()
    assert (na, nb) == ("alice", "bob")
    assert run(a, "alice")[2] == "alice"          # a restart of alice still sees only alice's data
    assert not (ROOT / "backend" / "fitness.db").exists()
