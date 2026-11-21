"""Wearable ingest: validation, idempotency, unit normalisation, manual-wins, file import, delete."""
import pytest
from fastapi.testclient import TestClient

from fastapi import FastAPI
from backend import wearables as W
from backend.memory import db

_app = FastAPI()
_app.include_router(W.router)   # minimal app: independent of the rest of the stack
client = TestClient(_app)
D = "2026-11-18"


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("FITNESS_NOW", "2026-11-20T10:00:00")


def post(records, source="garmin", workouts=()):
    return client.post("/wearables/ingest", json={"source": source, "records": records, "workouts": list(workouts)})


def test_ingest_idempotent_and_units():
    recs = [{"date": D, "metric": "sleep_h", "value": 450, "unit": "min"}, {"date": D, "metric": "hrv_ms", "value": 0.05, "unit": "s"},
            {"date": D, "metric": "active_kcal", "value": 2000, "unit": "kJ"}]
    a = post(recs).json()
    assert a["inserted"] == 3 and a["rejected"] == 0
    d = W.daily(D, D)[D]
    assert d["sleep_h"]["value"] == 7.5 and d["hrv_ms"]["value"] == 50.0 and abs(d["active_kcal"]["value"] - 478.012) < 0.01
    assert d["sleep_h"]["source"] == "garmin" and d["sleep_h"]["imported_at"]
    b = post(recs).json()
    assert b["unchanged"] == 3 and b["inserted"] == 0
    c = post([{"date": D, "metric": "sleep_h", "value": 8}]).json()
    assert c["updated"] == 1
    assert len(W.daily(D, D)[D]) == 3


def test_rejects_not_clips():
    bad = [{"date": D, "metric": "resting_hr", "value": 400}, {"date": D, "metric": "sleep_h", "value": 30},
           {"date": "2030-01-01", "metric": "steps", "value": 5}, {"date": D, "metric": "weight", "value": 5},
           {"date": D, "metric": "steps", "value": 5, "unit": "furlongs"}, {"date": "nope", "metric": "steps", "value": 5},
           {"date": D, "metric": "steps", "value": 9000}]
    r = post(bad).json()
    assert r["rejected"] == 6 and r["inserted"] == 1 and len(r["rejected_detail"]) == 6
    assert list(W.daily(D, D)[D]) == ["steps"]


def test_validation_limits():
    assert client.post("/wearables/ingest", json={"source": "Bad Source!", "records": []}).status_code == 400
    many = [{"date": D, "metric": "steps", "value": 1}] * (W.MAX_RECORDS + 1)
    assert client.post("/wearables/ingest", json={"source": "x", "records": many}).status_code == 422
    assert client.post("/wearables/ingest", json={"source": "x", "records": [{"date": D, "metric": "steps", "value": "lots"}]}).status_code == 422


def test_workouts():
    w = {"start": "2026-11-18T07:00:00", "kind": "Running", "minutes": 40, "kcal": 400}
    assert post([], workouts=[w]).json()["workouts_inserted"] == 1
    assert post([], workouts=[w]).json()["workouts_updated"] == 1
    r = post([], workouts=[{**w, "minutes": 5000}, {**w, "start": "garbage"}]).json()
    assert r["rejected"] == 2
    assert W.status()["workouts"] == 1


def test_manual_checkin_wins_in_frames():
    from fitness_os.mem import frames
    post([{"date": "2026-11-19", "metric": "sleep_h", "value": 5}, {"date": "2026-11-18", "metric": "sleep_h", "value": 8},
          {"date": "2026-11-18", "metric": "resting_hr", "value": 52}])
    db.add_checkin(sleep_h=6.5, soreness=2, energy=4)
    with db.conn() as c:
        c.execute("UPDATE checkins SET ts='2026-11-19T08:00:00+00:00'")
    rows = {r["date"]: r for r in frames.build(days=5)}
    assert rows["2026-11-19"]["sleep_h"] == 6.5 and rows["2026-11-19"]["sleep_src"] == "manual"
    assert rows["2026-11-18"]["sleep_h"] == 8 and rows["2026-11-18"]["sleep_src"] == "garmin" and rows["2026-11-18"]["rhr"] == 52
    assert rows["2026-11-17"]["sleep_h"] is None
    assert rows["2026-11-19"]["readiness"] is not None


def test_recent_summary_empty_then_filled():
    assert W.recent_summary() == {}
    post([{"date": D, "metric": "resting_hr", "value": 50}])
    s = W.recent_summary()
    assert s["days"][D]["resting_hr"] == 50 and s["sources"] == ["garmin"]


APPLE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE HealthData [<!ELEMENT HealthData (Record|Workout)*>]>
<HealthData locale="en_US">
 <Record type="HKQuantityTypeIdentifierStepCount" unit="count" startDate="2026-11-18 08:00:00 +0000" endDate="2026-11-18 09:00:00 +0000" value="3000"/>
 <Record type="HKQuantityTypeIdentifierStepCount" unit="count" startDate="2026-11-18 10:00:00 +0000" endDate="2026-11-18 11:00:00 +0000" value="2500"/>
 <Record type="HKQuantityTypeIdentifierRestingHeartRate" unit="count/min" startDate="2026-11-18 08:00:00 +0000" endDate="2026-11-18 08:00:00 +0000" value="55"/>
 <Record type="HKQuantityTypeIdentifierHeartRateVariabilitySDNN" unit="ms" startDate="2026-11-18 03:00:00 +0000" endDate="2026-11-18 03:00:00 +0000" value="48"/>
 <Record type="HKCategoryTypeIdentifierSleepAnalysis" startDate="2026-11-17 23:00:00 +0000" endDate="2026-11-18 03:00:00 +0000" value="HKCategoryValueSleepAnalysisAsleepCore"/>
 <Record type="HKCategoryTypeIdentifierSleepAnalysis" startDate="2026-11-18 01:00:00 +0000" endDate="2026-11-18 06:00:00 +0000" value="HKCategoryValueSleepAnalysisAsleepDeep"/>
 <Record type="HKCategoryTypeIdentifierSleepAnalysis" startDate="2026-11-18 06:00:00 +0000" endDate="2026-11-18 06:30:00 +0000" value="HKCategoryValueSleepAnalysisAwake"/>
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30" durationUnit="min" totalEnergyBurned="300" totalEnergyBurnedUnit="kcal" startDate="2026-11-18 07:00:00 +0000" endDate="2026-11-18 07:30:00 +0000"/>
</HealthData>"""


def test_apple_health_import():
    r = client.post("/wearables/import?format=apple_health", content=APPLE.encode(), headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 200, r.text
    d = W.daily(D, D)[D]
    assert d["steps"]["value"] == 5500 and d["resting_hr"]["value"] == 55 and d["hrv_ms"]["value"] == 48
    assert d["sleep_h"]["value"] == 7.0                       # 23:00-06:00 union, overlap not double counted, awake excluded
    assert d["sleep_h"]["source"] == "apple_health" and W.status()["workouts"] == 1
    again = client.post("/wearables/import?format=apple_health", content=APPLE.encode()).json()
    assert again["inserted"] == 0 and again["unchanged"] == 4 and again["workouts_updated"] == 1


def test_apple_health_refuses_entities_and_garbage():
    evil = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><HealthData>&a;</HealthData>'
    assert client.post("/wearables/import?format=apple_health", content=evil.encode()).status_code == 400
    assert client.post("/wearables/import?format=apple_health", content=b"not xml").status_code == 400
    assert client.post("/wearables/import?format=apple_health", content=b"").status_code == 400
    assert client.post("/wearables/import?format=zip", content=b"x").status_code == 400


def test_csv_import_and_oversize(monkeypatch):
    csv_text = "Calendar Date,Sleep Minutes,Resting HR,Steps,HRV\n2026-11-18,420,51,8000,60\n2026-11-19,420,500,abc,61\n"
    r = client.post("/wearables/import?format=csv&source=garmin", content=csv_text.encode()).json()
    assert r["inserted"] == 6 and r["rejected"] == 2
    assert W.daily(D, D)[D]["sleep_h"]["value"] == 7.0
    assert client.post("/wearables/import?format=csv", content=b"foo,bar\n1,2\n").status_code == 400
    monkeypatch.setitem(W.MAX_UPLOAD, "csv", 10)
    assert client.post("/wearables/import?format=csv", content=csv_text.encode()).status_code == 413


def test_delete_and_status():
    assert client.get("/wearables/status").json()["connected"] is False
    post([{"date": D, "metric": "steps", "value": 1}], source="garmin")
    post([{"date": D, "metric": "steps", "value": 2}], source="oura")
    assert client.get("/wearables/status").json()["connected"] is True
    assert client.delete("/wearables/data?source=garmin").json()["daily_rows"] == 1
    assert [s["source"] for s in client.get("/wearables/status").json()["sources"]] == ["oura"]
    assert client.delete("/wearables/data").json()["daily_rows"] == 1
    assert client.get("/wearables/status").json()["connected"] is False
