#!/usr/bin/env python3
"""Manage the people on this host.  python deploy/local/users.py add|list|rotate|remove [name]
Each person: users/<name>.json {name, port, token, data_dir} + users/<name>.txt (address placeholder + key to share).
The key is printed ONCE on add/rotate. Everything under users/ and data/ is git-ignored. `remove` keeps the data dir."""
from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402


def _write(u: dict) -> Path:
    d = common.users_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{u['name']}.json").write_text(json.dumps(u, indent=2), encoding="utf-8")
    txt = d / f"{u['name']}.txt"
    txt.write_text(
        f"Hi {u['name']}! Open the app, go to 'Connect to your coach' and paste:\n\n"
        f"Server address: <PUBLIC-ADDRESS-FROM-OWNER, e.g. https://pc.tailnet.ts.net>\n"
        f"Access key:     {u['token']}\n\nKeep the key private; it is your login.\n", encoding="utf-8")
    return txt


def cmd_add(name: str) -> int:
    if not common.NAME_RE.match(name):
        print("name must be 1-30 chars of a-z, 0-9, '-' (not at the ends)", file=sys.stderr)
        return 2
    users = common.load_users()
    if name in users:
        print(f"{name} already exists (use rotate to issue a new key)", file=sys.stderr)
        return 1
    port = max([u["port"] for u in users.values()] + [common.first_port() - 1]) + 1
    data_dir = common.data_root() / name
    data_dir.mkdir(parents=True, exist_ok=True)
    u = {"name": name, "port": port, "token": secrets.token_urlsafe(32), "data_dir": str(data_dir)}
    txt = _write(u)
    print(f"Added {name}: backend port {port}, data in {data_dir}")
    print(f"Access key (shown once; also in {txt}):\n  {u['token']}")
    print("A running host picks the new person up automatically (gateway hot-reloads; run.py starts the backend within seconds).")
    return 0


def cmd_list() -> int:
    users = common.load_users()
    if not users:
        print("no users yet: python deploy/local/users.py add <name>")
    for n, u in users.items():
        print(f"{n:<20} port {u['port']}  data {u.get('data_dir', '')}")
    return 0


def cmd_rotate(name: str) -> int:
    users = common.load_users()
    if name not in users:
        print(f"no such user {name}", file=sys.stderr)
        return 1
    u = users[name]
    u["token"] = secrets.token_urlsafe(32)
    txt = _write(u)
    print(f"New access key for {name} (shown once; also in {txt}):\n  {u['token']}")
    print("The old key stops working at once at the gateway; run.py restarts that backend within seconds.")
    return 0


def cmd_remove(name: str) -> int:
    users = common.load_users()
    if name not in users:
        print(f"no such user {name}", file=sys.stderr)
        return 1
    d = common.users_dir()
    for ext in ("json", "txt"):
        (d / f"{name}.{ext}").unlink(missing_ok=True)
    print(f"Removed {name}. Their data is kept in {users[name].get('data_dir')} (delete it yourself if wanted).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("action", choices=["add", "list", "rotate", "remove"])
    ap.add_argument("name", nargs="?")
    a = ap.parse_args()
    if a.action == "list":
        return cmd_list()
    if not a.name:
        ap.error(f"{a.action} needs a name")
    name = a.name.strip().lower()
    return {"add": cmd_add, "rotate": cmd_rotate, "remove": cmd_remove}[a.action](name)


if __name__ == "__main__":
    sys.exit(main())
