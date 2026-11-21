"""The background scheduler loop: starts once, survives failing ticks, can be disabled."""
import time

import pytest

from fitness_os import runner


@pytest.fixture(autouse=True)
def clean():
    runner.stop()
    yield
    runner.stop()


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_ticks_repeatedly_and_start_is_idempotent():
    calls = []
    assert runner.start(interval=0.02, first_delay=0, tick=lambda: calls.append(1)) is True
    assert runner.start(interval=0.02, first_delay=0, tick=lambda: calls.append(1)) is False       # already running
    assert wait_for(lambda: len(calls) >= 3) and runner.running()
    st = runner.status()
    assert st["enabled"] and st["ticks"] >= 3 and st["last_tick"] and st["last_error"] is None
    runner.stop()
    assert not runner.running()


def test_a_failing_tick_is_recorded_and_the_loop_keeps_going():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("model overloaded")
    runner.start(interval=0.02, first_delay=0, tick=flaky)
    assert wait_for(lambda: len(calls) >= 3)
    assert runner.running()
    runner.stop()


def test_can_be_disabled(monkeypatch):
    monkeypatch.setenv("FITNESS_SCHEDULER", "0")
    assert runner.start(interval=0.02, first_delay=0, tick=lambda: None) is False and not runner.running()
    assert runner.status()["enabled"] is False


def test_status_endpoint_reports_the_runner(monkeypatch):
    from fastapi.testclient import TestClient
    from backend.main import app
    runner.start(interval=0.02, first_delay=0, tick=lambda: None)
    wait_for(lambda: runner.status()["ticks"] >= 1)
    body = TestClient(app).get("/agents/scheduler").json()
    assert body["runner"]["running"] is True and "jobs" in body
