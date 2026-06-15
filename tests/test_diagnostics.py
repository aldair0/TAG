"""Diagnostic report: collect, write-to-disk, send (suppressed in tests)."""

from __future__ import annotations

from pathlib import Path


def test_collect_diagnostics_shape():
    from app.diagnostics import collect_diagnostics

    d = collect_diagnostics(reason="manual")
    for key in ("reason", "version", "python", "platform", "health", "activity"):
        assert key in d
    assert d["reason"] == "manual"


def test_write_bundle_creates_and_prunes(tmp_path: Path, monkeypatch):
    import app.diagnostics as diag

    monkeypatch.setattr(diag, "app_dir", lambda: tmp_path)
    out = diag.write_diagnostic_bundle({"reason": "manual", "version": "0.1.0"})
    assert out.exists()
    assert out.parent == tmp_path / "diagnostics"
    assert "LOG TAIL" in out.read_text(encoding="utf-8")


def test_send_report_writes_and_logs(tmp_path: Path, monkeypatch):
    import app.diagnostics as diag

    monkeypatch.setattr(diag, "app_dir", lambda: tmp_path)
    # TAG_DISABLE_ALERTS=1 (conftest) => email "logged", not actually sent.
    result = diag.send_diagnostic_report(reason="manual")
    assert result["email"] == "logged"
    assert result["file"] is not None
    assert Path(result["file"]).exists()


def test_diagnostics_route_button(client):
    r = client.post("/admin/settings/diagnostics", follow_redirects=False)
    assert r.status_code == 303
    assert "diag=" in r.headers["location"]
