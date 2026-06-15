"""Self-signed cert generation for direct-uvicorn HTTPS."""

from __future__ import annotations

from pathlib import Path

from app.tls import ensure_self_signed_cert


def test_generates_cert_and_key(tmp_path: Path):
    cert = tmp_path / "c.pem"
    key = tmp_path / "k.pem"
    out_cert, out_key = ensure_self_signed_cert(cert, key)
    assert out_cert == cert and out_key == key
    assert cert.exists() and key.exists()
    assert cert.read_bytes().startswith(b"-----BEGIN CERTIFICATE-----")
    assert b"PRIVATE KEY" in key.read_bytes()


def test_is_idempotent(tmp_path: Path):
    cert = tmp_path / "c.pem"
    key = tmp_path / "k.pem"
    ensure_self_signed_cert(cert, key)
    first = cert.read_bytes()
    # Second call must NOT regenerate (same bytes).
    ensure_self_signed_cert(cert, key)
    assert cert.read_bytes() == first


def test_cert_has_localhost_san(tmp_path: Path):
    from cryptography import x509

    cert_path, _ = ensure_self_signed_cert(tmp_path / "c.pem", tmp_path / "k.pem")
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    dns = san.get_values_for_type(x509.DNSName)
    assert "localhost" in dns
