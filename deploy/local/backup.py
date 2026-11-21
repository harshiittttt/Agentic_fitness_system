#!/usr/bin/env python3
r"""Consistent online backup of every person's fitness.db (sqlite3 backup API, safe while the backend runs).
python deploy/local/backup.py [--keep 14]  ->  deploy/local/backups/<name>/<YYYYmmdd-HHMMSS>.db ; keeps the newest N.
Schedule it daily with Task Scheduler (schtasks /Create /SC DAILY /ST 03:00 /TN AICoachBackup /TR "python <repo>\deploy\local\backup.py").
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402


def backup_user(name: str, data_dir: Path, keep: int) -> Path | None:
    src_path = data_dir / "fitness.db"
    if not src_path.is_file():
        print(f"  {name}: no fitness.db yet, skipped")
        return None
    dest_dir = common.backups_dir() / name
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{time.strftime('%Y%m%d-%H%M%S')}.db"
    src = sqlite3.connect(f"file:{src_path.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    for old in sorted(dest_dir.glob("*.db"))[:-keep] if keep > 0 else []:
        old.unlink()
    print(f"  {name}: {dest.name} ({dest.stat().st_size // 1024} KB)")
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--keep", type=int, default=14)
    a = ap.parse_args()
    users = common.load_users()
    if not users:
        print("no users")
        return 0
    for name, u in users.items():
        backup_user(name, Path(u.get("data_dir") or common.data_root() / name), a.keep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
