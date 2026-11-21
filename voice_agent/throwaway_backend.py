"""A throw-away copy of the coach backend on port 8011 with demo data in a temp folder, so voice tests never touch a real person's log.
Run:  python voice_agent/throwaway_backend.py      token: testtoken"""
import os
import re
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DATA = os.environ.get("TEST_DATA_DIR", "D:/voice-proto/testdata")
shutil.rmtree(DATA, ignore_errors=True)
os.makedirs(DATA, exist_ok=True)
os.environ["FITNESS_DATA_DIR"] = DATA
os.environ["API_TOKEN"] = "testtoken"
for line in open(os.path.join(ROOT, "deploy", "shared.env"), encoding="utf-8"):         # the model keys, never printed
    m = re.match(r"\s*([A-Z0-9_]+)=(.*)$", line)
    if m and m.group(2).strip():
        os.environ.setdefault(m.group(1), m.group(2).strip())

from backend.agents import orchestrator as orch   # noqa: E402
from backend.tools import demo                    # noqa: E402

orch.onboard({**demo.PROFILE, "name": "Golu", "coach_persona": "mentor", "days_per_week": 4})
import uvicorn                                    # noqa: E402

uvicorn.run("backend.main:app", host="127.0.0.1", port=int(os.environ.get("TEST_PORT", "8011")), log_level="warning")
