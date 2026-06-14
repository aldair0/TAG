from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_admin_index():
    r = client.get("/admin/")
    assert r.status_code == 200
    assert "TAG Collects" in r.text
    assert "Phase" in r.text


def test_root_redirects_to_admin():
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"].endswith("/admin/")
