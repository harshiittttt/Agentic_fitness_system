from fastapi.testclient import TestClient

from backend.main import app

c = TestClient(app)


def test_unset_token_is_open(monkeypatch):
    monkeypatch.delenv("API_TOKEN", raising=False)
    assert c.get("/health").status_code == 200
    assert c.get("/profile").status_code != 401


def test_blank_token_is_open(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "  ")
    assert c.get("/profile").status_code != 401


def test_token_required_except_health(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "s3cret-test")
    assert c.get("/health").status_code == 200
    r = c.get("/profile")
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert c.get("/agents/status", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get("/profile", headers={"Authorization": "s3cret-test"}).status_code == 401
    assert c.get("/profile", headers={"Authorization": "Bearer s3cret-test"}).status_code != 401
    assert c.post("/chat", json={"message": "hi"}).status_code == 401


def test_preflight_and_cors_on_401(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "s3cret-test")
    r = c.options("/profile", headers={"Origin": "http://x", "Access-Control-Request-Method": "GET"})
    assert r.status_code == 200
    r = c.get("/profile", headers={"Origin": "http://x"})
    assert r.status_code == 401 and r.headers.get("access-control-allow-origin") == "*"
