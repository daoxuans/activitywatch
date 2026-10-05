"""Optional, real TLS loopback test of the default urllib upload transport.

No external address is contacted. The certificate is trusted only while this
test runs; production certificate verification is never disabled.
"""

import ipaddress
import json
import os
import queue
import ssl
import threading
import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from aw_share.aggregate import summarize_day
from aw_share.state import SharingState
from aw_share.upload import SummaryUploader

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
except ImportError:  # The runtime client itself has no cryptography dependency.
    x509 = None


CHINA_TIME = timezone(timedelta(hours=8))
DAY = date(2026, 10, 5)


def local(hour, minute=0):
    return datetime(DAY.year, DAY.month, DAY.day, hour, minute, tzinfo=CHINA_TIME)


def event(start, seconds, data, **extra):
    return {
        "timestamp": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "duration": seconds,
        "data": data,
        **extra,
    }


def certificate_pair():
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    return (
        certificate.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


@unittest.skipUnless(x509 is not None, "optional cryptography package is absent")
class RealHttpsLoopbackTests(unittest.TestCase):
    def test_default_transport_posts_redacted_summary_over_verified_tls(self):
        folder = Path(__file__).resolve().parent
        unique = uuid.uuid4().hex
        cert_path = folder / f".https-test-{unique}.crt"
        key_path = folder / f".https-test-{unique}.key"
        state_path = folder / f".https-test-{unique}.json"
        lock_path = Path(str(state_path) + ".lock")
        for path in (cert_path, key_path, state_path, lock_path):
            self.addCleanup(path.unlink, missing_ok=True)

        certificate, private_key = certificate_pair()
        cert_path.write_bytes(certificate)
        key_path.write_bytes(private_key)

        received = queue.Queue(maxsize=1)

        class Receiver(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                received.put((self.path, dict(self.headers), self.rfile.read(length)))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def log_message(self, format, *args):
                # Test payloads and credentials should not appear in logs.
                pass

        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        except OSError as exc:
            self.skipTest(f"loopback bind unavailable: {type(exc).__name__}")
        self.addCleanup(server.server_close)

        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(str(cert_path), str(key_path))
        server.socket = server_context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        # addCleanup runs in reverse order: shutdown, join, close, delete files.
        self.addCleanup(thread.join, timeout=5)
        self.addCleanup(server.shutdown)

        endpoint = f"https://127.0.0.1:{server.server_port}/v1/summary"
        state = SharingState.load(state_path)
        state.enable(at=local(9), endpoint=endpoint)
        allowed = state.allowed_intervals(local(9), local(9, 20), now=local(9, 20))
        summary = summarize_day(
            day=DAY,
            timezone=CHINA_TIME,
            now=local(9, 20),
            window_events=[
                event(
                    local(9), 600,
                    {"app": r"C:\Users\Alice\Private\Editor.exe", "title": "TOP_SECRET_TITLE"},
                ),
                event(local(9, 10), 600, {"app": "chrome.exe", "title": "LOCAL_ONLY"}),
            ],
            afk_events=[event(local(9), 1200, {"status": "not-afk"})],
            web_events=[
                event(
                    local(9, 10), 600,
                    {
                        "url": "https://alice:password@study.example/private/course"
                        "?secret=TOP_SECRET_QUERY",
                        "title": "TOP_SECRET_WEB_TITLE",
                    },
                    bucket_id="aw-watcher-web-chrome",
                )
            ],
            allowed_intervals=allowed,
            app_categories={"Editor.exe": "学习办公"},
            domain_categories={"study.example": "学习网站"},
        )
        uploader = SummaryUploader(endpoint, token="TEST_ONLY_BEARER_TOKEN", max_retries=0)

        # OpenSSL trusts this newly generated test certificate only within
        # the request below. The default urllib HTTPS path still verifies the
        # certificate chain and the 127.0.0.1 SAN hostname/IP match.
        with patch.dict(
            os.environ,
            {
                "SSL_CERT_FILE": str(cert_path),
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
            },
        ):
            report_id = uploader.upload(
                summary,
                device_id=state.device_id,
                state=state,
                expected_generation=state.consent_generation,
            )

        path, headers, body = received.get(timeout=2)
        report = json.loads(body)
        self.assertEqual(path, "/v1/summary")
        self.assertEqual(headers["Authorization"], "Bearer TEST_ONLY_BEARER_TOKEN")
        self.assertEqual(headers["Idempotency-Key"], report_id)
        self.assertEqual(report["report_id"], report_id)
        self.assertEqual(report["summary"]["applications"]["Editor.exe"], 600)
        self.assertEqual(report["summary"]["domains"], {"study.example": 600})
        for forbidden in (
            "TOP_SECRET_TITLE",
            "TOP_SECRET_WEB_TITLE",
            "TOP_SECRET_QUERY",
            "password",
            "Alice",
            "/private/course",
            '"events"',
            '"timestamp"',
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, body.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
