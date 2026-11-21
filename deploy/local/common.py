"""Shared paths and helpers for deploy/local (stdlib only).  DEPLOY_LOCAL_HOME relocates users/, data/, logs/, backups/,
run/ and shared.env (used by the tests); DEPLOY_LOCAL_DATA_ROOT alone relocates just the per-person data dirs (e.g. to D:)."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
ROOT = CODE_DIR.parent.parent
NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,28}[a-z0-9])?$")


def home() -> Path:
    return Path(os.environ.get("DEPLOY_LOCAL_HOME") or CODE_DIR).resolve()


def users_dir() -> Path:
    return home() / "users"


def data_root() -> Path:
    return Path(os.environ.get("DEPLOY_LOCAL_DATA_ROOT") or (home() / "data")).resolve()


def logs_dir() -> Path:
    return home() / "logs"


def backups_dir() -> Path:
    return home() / "backups"


def run_dir() -> Path:
    return home() / "run"


def first_port() -> int:
    return int(os.environ.get("DEPLOY_LOCAL_FIRST_PORT") or 8101)


def gateway_port() -> int:
    return int(os.environ.get("GATEWAY_PORT") or 8000)


def voice_port() -> int:
    """The live voice agent (voice_agent/server.py, GPU) listens here, on 127.0.0.1 only; the gateway is its public door."""
    return int(os.environ.get("VOICE_PORT") or 8765)


def load_users(directory: Path | None = None) -> dict[str, dict]:
    """name -> {name, port, token, data_dir}; skips unreadable/incomplete files."""
    out: dict[str, dict] = {}
    d = directory or users_dir()
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            u = json.loads(p.read_text(encoding="utf-8"))
            if u.get("token") and isinstance(u.get("port"), int):
                u.setdefault("name", p.stem)
                out[p.stem] = u
        except (OSError, ValueError):
            continue
    return out


def parse_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        env[k.strip()] = v
    return env
