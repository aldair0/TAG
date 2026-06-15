"""Self-signed TLS certificate for direct-uvicorn HTTPS on the LAN.

We serve HTTPS straight from uvicorn (no reverse proxy). On first run this
generates a long-lived self-signed cert under ``TAG_HOME/certs`` covering
localhost, the machine hostname, and its LAN IP, so the operator never has to
touch openssl. To get a clean padlock (no browser warning), install
``tag-cert.pem`` as a trusted certificate on the staff tablet once.

Idempotent: an existing cert/key pair is reused as-is.
"""

from __future__ import annotations

import datetime
import ipaddress
import logging
import socket
from pathlib import Path

from app.paths import app_dir

logger = logging.getLogger(__name__)


def cert_paths() -> tuple[Path, Path]:
    d = app_dir() / "certs"
    return d / "tag-cert.pem", d / "tag-key.pem"


def _local_sans():
    """SAN entries: localhost, 127.0.0.1, the hostname, and the LAN IP."""
    from cryptography import x509

    sans = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]
    try:
        hostname = socket.gethostname()
        if hostname:
            sans.append(x509.DNSName(hostname))
        lan_ip = socket.gethostbyname(hostname)
        ip = ipaddress.ip_address(lan_ip)
        if not ip.is_loopback:
            sans.append(x509.IPAddress(ip))
    except Exception:
        logger.debug("Could not resolve LAN hostname/IP for cert SAN", exc_info=True)
    return sans


def ensure_self_signed_cert(
    certfile: str | Path | None = None,
    keyfile: str | Path | None = None,
    *,
    validity_days: int = 3650,
) -> tuple[Path, Path]:
    """Return (certfile, keyfile), generating a self-signed pair if missing."""
    default_cert, default_key = cert_paths()
    certfile = Path(certfile) if certfile else default_cert
    keyfile = Path(keyfile) if keyfile else default_key

    if certfile.exists() and keyfile.exists():
        return certfile, keyfile

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    certfile.parent.mkdir(parents=True, exist_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    hostname = socket.gethostname() or "tag-inventory"
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.datetime.now(datetime.timezone.utc)

    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=validity_days))
        .add_extension(x509.SubjectAlternativeName(_local_sans()), critical=False)
        # ca=True so the same cert can be installed as a trusted root on the
        # tablet; serverAuth EKU so modern browsers accept it for TLS.
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(key, hashes.SHA256())
    )

    keyfile.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    try:
        keyfile.chmod(0o600)  # best-effort; no-op semantics on some Windows setups
    except OSError:
        pass

    logger.info(
        "Generated self-signed TLS cert %s (valid %d days). Install it on the "
        "tablet as a trusted cert for a warning-free padlock.",
        certfile, validity_days,
    )
    return certfile, keyfile
