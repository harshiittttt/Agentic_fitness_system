# AI Fitness Agentic System

Voice-first gym coach. Four agents (Training, Nutrition, Recovery, Progress Analyst) plus an orchestrator.
Adaptation is continuous and evidence-weighted: small signals give small nudges, strong persistent signals
give bigger changes, safety flags act immediately. No trained ML models; numbers come from deterministic tools.

## Run the backend
```
pip install -r requirements.txt
copy .env.example .env      # add ANTHROPIC_API_KEY (optional: logging/plans/review work offline)
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
python -m pytest backend/tests -q
```

## Run the mobile app
```
cd mobile
copy .env.example .env      # set EXPO_PUBLIC_API_URL to your laptop's LAN IP
npm start                   # scan the QR with Expo Go
```
Voice recognition needs a dev build (`npx expo run:android`); in plain Expo Go the app falls back to typed input.

## How adaptation works
`backend/policy/adaptation.py`: signal strength x confidence x change-dampening, with a small dead zone to avoid jitter.
`POST /review` runs the agent debate; every applied change is written to the change log with reason and evidence.

## 12-week simulation
`python -m backend.sim.run` (~3 min) runs the real agents against a simulated lifter (hidden strength + fatigue, noisy RPE)
over 5 scenarios and compares each against a control where the plan never adapts. Regression tests: `backend/tests/test_sim.py`.
The simulated lifter is a test dummy written by hand, not trained on real data, so it checks that the logic behaves as designed,
not that it works for real people.
