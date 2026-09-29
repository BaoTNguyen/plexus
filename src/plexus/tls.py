"""CA and proxy TLS material for the egress proxy's MITM.

Lives in seat_secrets()/"tls": a self-signed CA (name-constrained to the
proxy's own DNS names, so a leaked CA key can't mint certs for the open
internet) and a proxy leaf cert it signs. sandbox.py decides whether a
missing or expiring cert is worth fixing; this module only reports
(status) or does (provision) -- it never fixes on its own.
"""
from __future__ import annotations

import datetime
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .registry import seat_secrets

_EXPIRY_WARNING_DAYS = 30
_CA_DAYS = 1825
_PROXY_DAYS = 365


def _tls_dir(dir: Path | None) -> Path:
    return Path(dir) if dir is not None else seat_secrets() / "tls"


def _openssl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["openssl", *args], capture_output=True, text=True)


def _not_after(cert: Path) -> datetime.datetime | None:
    if not cert.is_file():
        return None
    proc = _openssl("x509", "-enddate", "-noout", "-in", str(cert))
    if proc.returncode != 0:
        return None
    # "notAfter=Sep 29 00:00:00 2026 GMT" -- always GMT, so drop it and assume UTC.
    raw = proc.stdout.strip().split("=", 1)[1].removesuffix(" GMT")
    return datetime.datetime.strptime(raw, "%b %d %H:%M:%S %Y").replace(tzinfo=datetime.timezone.utc)


def _days_left(cert: Path) -> float | None:
    not_after = _not_after(cert)
    if not_after is None:
        return None
    return (not_after - datetime.datetime.now(datetime.timezone.utc)).total_seconds() / 86400


def _cert_status(cert: Path) -> str:
    days = _days_left(cert)
    if days is None:
        return "missing"
    not_after = _not_after(cert)
    label = f"expires {not_after:%Y-%m-%d}"
    return f"expiring ({label})" if days < _EXPIRY_WARNING_DAYS else f"ok ({label})"


def _ca_status(dir: Path) -> str:
    return _cert_status(dir / "ca.pem")


def status(dir: Path | None = None) -> list[str]:
    """Read-only report, two lines: 'tls ca: ...' and 'tls proxy: ...'."""
    if not shutil.which("openssl"):
        return ["tls: MISSING -- openssl not installed"]
    d = _tls_dir(dir)
    return [
        f"tls ca: {_ca_status(d)}",
        f"tls proxy: {_cert_status(d / 'proxy.pem')}",
    ]


def _write_via_tmp(dir: Path, name: str, mode: int, run) -> None:
    """Run openssl with its output pointed at a private tmp file in `dir`,
    then rename into place -- a failed run never leaves a half-written cert."""
    fd, tmp = tempfile.mkstemp(dir=dir, prefix=f".{name}.")
    os.close(fd)
    try:
        proc = run(tmp)
        if proc.returncode != 0:
            raise RuntimeError(f"openssl failed writing {name}: {proc.stderr.strip()}")
        os.chmod(tmp, mode)
        os.replace(tmp, dir / name)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _make_ca(d: Path) -> None:
    _write_via_tmp(d, "ca.key", 0o600, lambda out: _openssl(
        "ecparam", "-genkey", "-name", "prime256v1", "-noout", "-out", out,
    ))
    _write_via_tmp(d, "ca.pem", 0o644, lambda out: _openssl(
        "req", "-x509", "-new", "-key", str(d / "ca.key"), "-days", str(_CA_DAYS),
        "-subj", "/CN=plexus egress CA",
        "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        "-addext", "nameConstraints=critical,permitted;DNS:egress,permitted;DNS:egress-web",
        "-out", out,
    ))


def _make_proxy_cert(d: Path) -> None:
    _write_via_tmp(d, "proxy.key", 0o600, lambda out: _openssl(
        "ecparam", "-genkey", "-name", "prime256v1", "-noout", "-out", out,
    ))
    csr_fd, csr = tempfile.mkstemp(dir=d, prefix=".proxy.csr.")
    os.close(csr_fd)
    ext_fd, ext = tempfile.mkstemp(dir=d, prefix=".proxy.ext.")
    os.close(ext_fd)
    Path(ext).write_text(
        "subjectAltName=DNS:egress,DNS:egress-web\nextendedKeyUsage=serverAuth\n"
    )
    try:
        proc = _openssl(
            "req", "-new", "-key", str(d / "proxy.key"), "-subj", "/CN=egress", "-out", csr,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"openssl failed writing proxy CSR: {proc.stderr.strip()}")
        _write_via_tmp(d, "proxy.pem", 0o644, lambda out: _openssl(
            "x509", "-req", "-in", csr, "-CA", str(d / "ca.pem"), "-CAkey", str(d / "ca.key"),
            "-CAcreateserial", "-days", str(_PROXY_DAYS), "-extfile", ext, "-out", out,
        ))
    finally:
        Path(csr).unlink(missing_ok=True)
        Path(ext).unlink(missing_ok=True)


def provision(dir: Path | None = None) -> None:
    """Create or renew whatever is missing or stale. Idempotent: a second
    call with everything already fresh touches nothing."""
    if not shutil.which("openssl"):
        raise RuntimeError("openssl not installed")
    d = _tls_dir(dir)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)

    ca_created = not (d / "ca.key").is_file() or not (d / "ca.pem").is_file()
    if ca_created:
        _make_ca(d)

    proxy_days = _days_left(d / "proxy.pem")
    proxy_stale = proxy_days is None or proxy_days < _EXPIRY_WARNING_DAYS
    if ca_created or proxy_stale or not (d / "proxy.key").is_file():
        _make_proxy_cert(d)
