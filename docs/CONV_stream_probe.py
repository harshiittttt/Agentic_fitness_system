"""Does /convo/turn really stream over HTTP (lines arrive while the model is still talking), and what is the latency from the client's side?
Starts a throw-away backend with demo data on port 8031 (never the real database), sends a few turns with the real model, prints each event with its arrival time.
    python docs/CONV_stream_probe.py"""
import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["FITNESS_DATA_DIR"] = tempfile.mkdtemp()
os.environ["VOICE_BRAIN"] = "converse"
os.environ["CONVO_LOG"] = "0"
import httpx  # noqa: E402
import uvicorn  # noqa: E402

from backend.agents import orchestrator as orch  # noqa: E402
from backend.tools import demo  # noqa: E402

orch.onboard({**demo.PROFILE, "name": "Golu", "coach_persona": "hype", "days_per_week": 4})
from backend.main import app  # noqa: E402

PORT = 8031
server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
for _ in range(100):
    try:
        if httpx.get(f"http://127.0.0.1:{PORT}/health", timeout=1).status_code == 200:
            break
    except httpx.HTTPError:
        time.sleep(0.1)

c = httpx.Client(base_url=f"http://127.0.0.1:{PORT}", timeout=httpx.Timeout(20.0, read=12.0))
print("status:", c.get("/convo/status").json())
for i, text in enumerate(["sixty kilos five reps done", "how many sets are left", "we are not skipping anything, we are doing every exercise", "what is a good cue for squats"]):
    t0 = time.time()
    print(f"\n> {text!r}")
    with c.stream("POST", "/convo/turn", json={"text": text, "turn_id": f"p{i}", "conn_id": "probe"}) as r:
        for line in r.iter_lines():
            if not line:
                continue
            e = json.loads(line)
            body = {k: v for k, v in e.items() if k in ("kind", "text", "name", "status", "action", "intent", "brain", "said")}
            print(f"  +{(time.time() - t0) * 1000:6.0f} ms  {e['t']:6} {json.dumps(body, ensure_ascii=False)[:150]}")
server.should_exit = True
