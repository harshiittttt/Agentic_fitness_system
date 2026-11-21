#!/usr/bin/env python3
"""Start the whole local host:  python deploy/local/run.py [start]   |   python deploy/local/run.py status
One uvicorn backend per person (127.0.0.1:<their port>, one worker, own FITNESS_DATA_DIR + API_TOKEN) plus the gateway
(127.0.0.1:8000, or --port / GATEWAY_PORT). Crashed processes restart with backoff; new/rotated/removed people are picked
up while running. Ctrl+C stops every child. Logs: deploy/local/logs/<name>.log (5 MB x 3, rotated). Tokens are never logged."""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def say(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def http_ok(port: int, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
        return f'"{pid}"' in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def rss_mb(pid: int) -> float | None:
    """Resident memory in MB (psutil if installed, else tasklist on Windows)."""
    try:
        import psutil  # optional
        return psutil.Process(pid).memory_info().rss / 1048576
    except ImportError:
        pass
    except Exception:
        return None
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
        parts = [p.strip('"') for p in out.strip().split('","')]
        if len(parts) >= 5:
            digits = "".join(c for c in parts[4] if c.isdigit())
            return int(digits) / 1024 if digits else None
    return None


def kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


class Managed:
    """One supervised child process: restart with exponential backoff, output into a size-capped rotating log."""

    def __init__(self, name: str, argv: list[str], env: dict[str, str], port: int, sig: str):
        self.name, self.argv, self.env, self.port, self.sig = name, argv, env, port, sig
        self.proc: subprocess.Popen | None = None
        self.fails = 0
        self.next_start = 0.0
        self.started_at = 0.0
        common.logs_dir().mkdir(parents=True, exist_ok=True)
        self.log = logging.getLogger(f"proc.{name}.{id(self)}")
        self.log.propagate = False
        self.log.setLevel(logging.INFO)
        self.handler = logging.handlers.RotatingFileHandler(
            common.logs_dir() / f"{name}.log", maxBytes=5_000_000, backupCount=2, encoding="utf-8")
        self.handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        self.log.addHandler(self.handler)

    def start(self) -> None:
        self.proc = subprocess.Popen(
            self.argv, cwd=str(common.ROOT), env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW)
        self.started_at = time.time()
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
        self.log.info("--- started pid %s ---", self.proc.pid)

    def _pump(self, proc: subprocess.Popen) -> None:
        for line in iter(proc.stdout.readline, b""):
            self.log.info(line.decode("utf-8", "replace").rstrip())

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            kill_tree(self.proc.pid)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        self.log.removeHandler(self.handler)
        self.handler.close()

    def tick(self) -> None:
        """Restart a crashed process after backoff 2,4,8..60 s; a process that stayed up 60 s resets the counter."""
        if self.running():
            if self.fails and time.time() - self.started_at > 60:
                self.fails = 0
            return
        now = time.time()
        if self.proc is not None and self.next_start == 0.0:
            self.fails += 1
            wait = min(60, 2 ** self.fails)
            self.next_start = now + wait
            say(f"{self.name} exited (code {self.proc.returncode}); restart in {wait}s (see logs/{self.name}.log)")
        if self.proc is None or now >= self.next_start:
            self.next_start = 0.0
            self.start()
            say(f"{self.name} (re)started")


def backend_env(u: dict, shared: dict[str, str], n_users: int) -> dict[str, str]:
    env = dict(os.environ)
    env.update(shared)
    total = int(shared.get("ELEVEN_MONTHLY_CREDITS") or os.environ.get("ELEVEN_MONTHLY_CREDITS") or 9000)
    env.update({
        "FITNESS_DATA_DIR": u.get("data_dir") or str(common.data_root() / u["name"]),
        "API_TOKEN": u["token"],
        "ELEVEN_MONTHLY_CREDITS": str(max(1, total // max(1, n_users))),
        "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})
    env.pop("FITNESS_DB", None)  # an inherited explicit DB file would defeat the per-person data dir
    return env


def user_sig(u: dict) -> str:
    return f"{u['port']}|{u['token']}|{u.get('data_dir')}"  # only compared in memory, never printed


def write_state(gw_port: int, backends: dict[str, Managed], gw: Managed) -> None:
    common.run_dir().mkdir(parents=True, exist_ok=True)
    state = {"runner_pid": os.getpid(), "gateway": {"port": gw_port, "pid": gw.proc.pid if gw.proc else None},
             "backends": {n: {"port": m.port, "pid": m.proc.pid if m.proc else None} for n, m in backends.items()}}
    (common.run_dir() / "state.json").write_text(json.dumps(state), encoding="utf-8")


def cmd_status() -> int:
    sf = common.run_dir() / "state.json"
    if not sf.is_file():
        print("not running (no state file). Start with: python deploy/local/run.py")
        return 1
    st = json.loads(sf.read_text(encoding="utf-8"))
    if not pid_alive(st["runner_pid"]):
        print("not running (stale state file).")
        return 1
    print(f"runner pid {st['runner_pid']}")
    total = 0.0
    rows = [("gateway", st["gateway"])] + sorted(st["backends"].items())
    for name, info in rows:
        up = bool(info["pid"]) and http_ok(info["port"])
        mem = rss_mb(info["pid"]) if info["pid"] else None
        total += mem or 0
        print(f"  {name:<20} port {info['port']:<6} {'UP  ' if up else 'DOWN'} pid {info['pid']}  RSS {mem and round(mem) or '?'} MB")
    print(f"  total RSS ~{round(total)} MB (this PC has ~7.4 GB; Windows + browser use a lot of it)")
    return 0


def cmd_start(a) -> int:
    gw_port = a.port
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", gw_port)) == 0:
            print(f"port {gw_port} is already in use (is the host already running? try: run.py status)", file=sys.stderr)
            return 1
    shared = common.parse_env_file(common.home() / "shared.env")
    if not shared:
        say("note: no shared.env found, using the environment / repo .env for GEMINI_API_KEY etc.")
    backends: dict[str, Managed] = {}
    gw = Managed("gateway", [sys.executable, str(common.CODE_DIR / "gateway.py"), "--port", str(gw_port)],
                 {**os.environ, "PYTHONUNBUFFERED": "1", "DEPLOY_LOCAL_HOME": str(common.home())}, gw_port, "")
    stopping = {"v": False}

    def stop_all(*_):
        stopping["v"] = True

    signal.signal(signal.SIGINT, stop_all)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, stop_all)
    signal.signal(signal.SIGTERM, stop_all)

    def sync_users() -> None:
        users = common.load_users()
        for n in [n for n in backends if n not in users]:
            say(f"{n} removed: stopping backend")
            backends.pop(n).stop()
        for n, u in users.items():
            sig = user_sig(u)
            if n in backends and backends[n].sig != sig:
                say(f"{n} changed (new key/port): restarting backend")
                backends.pop(n).stop()
            if n not in backends:
                backends[n] = Managed(
                    n, [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", str(u["port"]),
                        "--workers", "1", "--log-level", "warning"], backend_env(u, shared, len(users)), u["port"], sig)

    try:
        sync_users()
        if not backends:
            say("no users yet; add one with: python deploy/local/users.py add <name> (they are picked up automatically)")
        for m in backends.values():
            m.tick()
        gw.tick()
        deadline = time.time() + 120
        pending = {n: m for n, m in backends.items()}
        while pending and time.time() < deadline and not stopping["v"]:
            for n, m in list(pending.items()):
                if http_ok(m.port):
                    say(f"{n}: backend healthy on {m.port}")
                    del pending[n]
            time.sleep(1)
        for n in pending:
            say(f"WARNING {n}: not healthy after 120 s, see logs/{n}.log")
        if http_ok(gw_port, 5):
            say(f"gateway healthy on http://127.0.0.1:{gw_port}  ({len(backends)} people). Ctrl+C to stop.")
        n_loops = 0
        while not stopping["v"]:
            time.sleep(1)
            n_loops += 1
            if n_loops % 3 == 0:
                sync_users()
            for m in list(backends.values()) + [gw]:
                m.tick()
            if n_loops % 3 == 0:
                write_state(gw_port, backends, gw)
    finally:
        say("stopping all processes ...")
        for m in list(backends.values()) + [gw]:
            m.stop()
        (common.run_dir() / "state.json").unlink(missing_ok=True)
        say("stopped.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("action", nargs="?", default="start", choices=["start", "status"])
    ap.add_argument("--port", type=int, default=common.gateway_port(), help="gateway port (default 8000)")
    a = ap.parse_args()
    return cmd_status() if a.action == "status" else cmd_start(a)


if __name__ == "__main__":
    sys.exit(main())
