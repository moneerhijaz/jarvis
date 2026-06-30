"""jarvisd entrypoint: start the local backend (and a tiny CLI)."""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from jarvis.app import Application
from jarvis.config import load_settings


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(prog="jarvisd", description="JARVIS local backend")
    sub = parser.add_subparsers(dest="cmd")
    serve_p = sub.add_parser("serve", help="run the API server (default)")
    serve_p.add_argument("--host", default=None, help="bind address (use 0.0.0.0 to expose on your LAN)")
    serve_p.add_argument("--port", type=int, default=None, help="port (default 8765)")
    serve_p.add_argument("--certfile", default=None, help="TLS cert (enables https — needed for mic access from other devices)")
    serve_p.add_argument("--keyfile", default=None, help="TLS private key (use with --certfile)")
    serve_p.add_argument("--self-signed", action="store_true",
                         help="auto-generate a temporary self-signed cert and serve https (for mobile mic). No files needed.")
    sub.add_parser("health", help="print model/backend health and exit")
    run_p = sub.add_parser("run", help="run a single task headless (uses LM Studio)")
    run_p.add_argument("task")
    run_p.add_argument("--cwd", default=None)
    run_p.add_argument("--project", default=None)
    args = parser.parse_args()

    settings = load_settings()

    if args.cmd == "health":
        app = Application(settings)
        app.ensure_vault()
        print(app.model.health().model_dump_json(indent=2))
        app.close()
        return

    if args.cmd == "run":
        app = Application(settings)
        app.ensure_vault()
        req = app.new_run(args.task, working_directory=args.cwd, project=args.project)
        res = asyncio.run(app.run_sync(req))
        print(res.model_dump_json(indent=2))
        app.close()
        return

    # default: serve
    import uvicorn

    from jarvis.api.server import create_app

    app = Application(settings)
    api = create_app(app)
    host = getattr(args, "host", None) or settings.app.host
    port = getattr(args, "port", None) or settings.app.port
    certfile = getattr(args, "certfile", None)
    keyfile = getattr(args, "keyfile", None)
    log = logging.getLogger("jarvis")
    lan = _lan_ip()
    if getattr(args, "self_signed", False) and not certfile:
        certfile, keyfile = _gen_self_signed_cert(lan)
        log.info("self-signed cert generated (browser will warn once -> accept it)")
    scheme = "https" if certfile else "http"
    shown = lan if host in ("0.0.0.0", "::") else host
    log.info("serving on %s://%s:%s  (open %s://%s:%s)", scheme, host, port, scheme, shown, port)
    uvicorn.run(api, host=host, port=port, ssl_certfile=certfile, ssl_keyfile=keyfile)


def _lan_ip() -> str:
    """Best-effort local network IP (no packets actually sent)."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _gen_self_signed_cert(lan_ip: str) -> tuple[str, str]:
    """Generate a throwaway self-signed cert (valid for localhost + this LAN IP) and
    return (certfile, keyfile) temp paths. Requires the 'cryptography' package."""
    try:
        import datetime
        import ipaddress
        import os
        import tempfile

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        raise SystemExit(
            "--self-signed needs the 'cryptography' package. Install it, then retry:\n"
            "  pip install cryptography"
        )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    san = [x509.DNSName("localhost")]
    for ip in ("127.0.0.1", lan_ip):
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except Exception:
            pass
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "JARVIS")])
    now = datetime.datetime.utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .sign(key, hashes.SHA256())
    )
    d = tempfile.mkdtemp(prefix="jarvis_cert_")
    cp, kp = os.path.join(d, "cert.pem"), os.path.join(d, "key.pem")
    with open(cp, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(kp, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.TraditionalOpenSSL,
                                  serialization.NoEncryption()))
    return cp, kp


if __name__ == "__main__":
    main()
