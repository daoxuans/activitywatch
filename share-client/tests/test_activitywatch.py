"""Synthetic tests for the read-only, loopback-only ActivityWatch adapter.

The sensitive fixture values are deliberately confined to local HTTP replies;
errors must not repeat them. No installed ActivityWatch or network service is
needed to run these tests.
"""

import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from aw_share.activitywatch import ActivityWatchClient, ActivityWatchError, _NoRedirect


CHINA_TIME = timezone(timedelta(hours=8))
START = datetime(2026, 10, 5, 23, 55, tzinfo=CHINA_TIME)
END = datetime(2026, 10, 6, 0, 5, tzinfo=CHINA_TIME)
INFO = {"hostname": "PC", "device_id": "local-device-id"}
BUCKETS = {
    "aw-watcher-window_PC": {"type": "currentwindow", "hostname": "PC"},
    "aw-watcher-afk_PC": {"type": "afkstatus", "hostname": "PC"},
    "aw-watcher-web-chrome_PC": {"type": "web.tab.current", "hostname": "PC"},
    "aw-watcher-web-edge_PC": {"type": "web.tab.current", "hostname": "PC"},
    "unrelated": {"type": "app.editor.activity", "hostname": "PC"},
}


def event(data):
    return {"timestamp": "2026-10-05T15:55:00Z", "duration": 600, "data": data}


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


class FakeOpener:
    def __init__(self, replies):
        self.replies = replies
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        reply = self.replies[urlsplit(request.full_url).path]
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, bytes):
            return FakeResponse(reply)
        return FakeResponse(json.dumps(reply).encode("utf-8"))


class ActivityWatchClientTests(unittest.TestCase):
    def setUp(self):
        # The adapter must behave as if this fixture server runs on the test
        # computer, regardless of the actual CI machine's hostname.
        local_name = patch("aw_share.activitywatch.socket.gethostname", return_value="PC")
        physical_name = patch(
            "aw_share.activitywatch._windows_physical_dns_hostname", return_value=None
        )
        local_name.start()
        physical_name.start()
        self.addCleanup(physical_name.stop)
        self.addCleanup(local_name.stop)

    def client(self, replies, **kwargs):
        client = ActivityWatchClient(**kwargs)
        opener = FakeOpener(replies)
        client._opener = opener
        return client, opener

    def test_reads_only_matching_bucket_types_and_preserves_browser_source(self):
        replies = {
            "/api/0/info": INFO,
            "/api/0/buckets/": BUCKETS,
            "/api/0/buckets/aw-watcher-window_PC/events": [
                event({"app": "chrome.exe", "title": "LOCAL_ONLY_WINDOW_TITLE"})
            ],
            "/api/0/buckets/aw-watcher-afk_PC/events": [
                event({"status": "not-afk"})
            ],
            "/api/0/buckets/aw-watcher-web-chrome_PC/events": [
                event({"url": "https://study.example/private/course"})
            ],
            "/api/0/buckets/aw-watcher-web-edge_PC/events": [
                event({"url": "https://video.example/watch"})
            ],
        }
        client, opener = self.client(replies)

        found = client.fetch_events(START, END)

        self.assertEqual(set(found), {"window", "afk", "web"})
        self.assertEqual(len(found["window"]), 1)
        self.assertEqual(len(found["afk"]), 1)
        self.assertEqual(
            {item["bucket_id"] for item in found["web"]},
            {"aw-watcher-web-chrome_PC", "aw-watcher-web-edge_PC"},
        )
        self.assertEqual(len(opener.requests), 6)
        self.assertFalse(any("unrelated" in req.full_url for req, _ in opener.requests))
        self.assertTrue(all(req.get_method() == "GET" for req, _ in opener.requests))
        for request, _ in opener.requests[2:]:
            query = parse_qs(urlsplit(request.full_url).query)
            self.assertEqual(query["start"], [START.isoformat()])
            self.assertEqual(query["end"], [END.isoformat()])
            self.assertEqual(query["limit"], ["-1"])

    def test_query_encodes_bucket_id_and_keeps_cross_day_offsets(self):
        path = "/api/0/buckets/aw-watcher-web%2Fedge_PC/events"
        client, opener = self.client({path: []})

        self.assertEqual(client.get_events("aw-watcher-web/edge_PC", START, END), [])

        request, timeout = opener.requests[0]
        parsed = urlsplit(request.full_url)
        self.assertEqual(parsed.path, path)
        self.assertEqual(
            parse_qs(parsed.query),
            {
                "start": ["2026-10-05T23:55:00+08:00"],
                "end": ["2026-10-06T00:05:00+08:00"],
                "limit": ["-1"],
            },
        )
        self.assertEqual(timeout, 5.0)

    def test_duplicate_window_bucket_fails_before_reading_events(self):
        buckets = {
            **BUCKETS,
            "aw-watcher-window_PC_copy": {"type": "currentwindow", "hostname": "PC"},
        }
        client, opener = self.client({"/api/0/info": INFO, "/api/0/buckets/": buckets})

        with self.assertRaisesRegex(ActivityWatchError, "exactly one.*window"):
            client.fetch_events(START, END)

        self.assertEqual(len(opener.requests), 2)

    def test_only_remote_window_and_afk_buckets_are_rejected_without_reading_them(self):
        private_host = "OTHER_PRIVATE_DEVICE"
        remote = {
            "remote-window": {"type": "currentwindow", "hostname": private_host},
            "remote-afk": {"type": "afkstatus", "hostname": private_host},
        }
        client, opener = self.client({"/api/0/info": INFO, "/api/0/buckets/": remote})

        with self.assertRaises(ActivityWatchError) as raised:
            client.fetch_events(START, END)

        self.assertEqual(len(opener.requests), 2)
        self.assertNotIn(private_host, str(raised.exception))

    def test_remote_and_unidentified_web_buckets_are_not_read(self):
        buckets = {
            **BUCKETS,
            # Device ID wins over a stale hostname for this local browser.
            "aw-watcher-web-firefox_OLD_NAME": {
                "type": "web.tab.current",
                "hostname": "OLD_NAME",
                "data": {"device_id": INFO["device_id"]},
            },
            # A cloned hostname must not override a mismatching device ID.
            "aw-watcher-web-chrome_OTHER": {
                "type": "web.tab.current",
                "hostname": "PC",
                "data": {"device_id": "other-device-id"},
            },
            "aw-watcher-web-unknown": {"type": "web.tab.current", "hostname": "unknown"},
            "remote-window": {"type": "currentwindow", "hostname": "OTHER"},
        }
        replies = {
            "/api/0/info": INFO,
            "/api/0/buckets/": buckets,
            "/api/0/buckets/aw-watcher-window_PC/events": [event({"app": "firefox.exe"})],
            "/api/0/buckets/aw-watcher-afk_PC/events": [event({"status": "not-afk"})],
            "/api/0/buckets/aw-watcher-web-chrome_PC/events": [],
            "/api/0/buckets/aw-watcher-web-edge_PC/events": [],
            "/api/0/buckets/aw-watcher-web-firefox_OLD_NAME/events": [
                event({"url": "https://local.example/"})
            ],
        }
        client, opener = self.client(replies)

        result = client.fetch_events(START, END)

        self.assertEqual(
            [item["bucket_id"] for item in result["web"]],
            ["aw-watcher-web-firefox_OLD_NAME"],
        )
        self.assertFalse(any("OTHER" in req.full_url or "unknown" in req.full_url for req, _ in opener.requests))

    def test_older_server_without_device_id_falls_back_to_hostname(self):
        replies = {
            "/api/0/info": {"hostname": "pc"},
            "/api/0/buckets/": BUCKETS,
            "/api/0/buckets/aw-watcher-window_PC/events": [event({"app": "Code.exe"})],
            "/api/0/buckets/aw-watcher-afk_PC/events": [event({"status": "not-afk"})],
            "/api/0/buckets/aw-watcher-web-chrome_PC/events": [],
            "/api/0/buckets/aw-watcher-web-edge_PC/events": [],
        }
        client, _ = self.client(replies)

        self.assertEqual(len(client.fetch_events(START, END)["window"]), 1)

    def test_loopback_tunnel_to_another_server_is_rejected_before_bucket_read(self):
        remote_name = "REMOTE_PRIVATE_HOSTNAME"
        client, opener = self.client({"/api/0/info": {"hostname": remote_name}})

        with self.assertRaises(ActivityWatchError) as raised:
            client.fetch_events(START, END)

        self.assertEqual(
            [urlsplit(request.full_url).path for request, _ in opener.requests],
            ["/api/0/info"],
        )
        self.assertNotIn(remote_name, str(raised.exception))
        self.assertNotIn("PC", str(raised.exception))

    def test_fqdn_and_trailing_dot_match_local_short_hostname(self):
        hostname = "pC.university.example."
        buckets = {key: {**value, "hostname": hostname} for key, value in BUCKETS.items()}
        replies = {
            "/api/0/info": {"hostname": hostname},
            "/api/0/buckets/": buckets,
            "/api/0/buckets/aw-watcher-window_PC/events": [event({"app": "Code.exe"})],
            "/api/0/buckets/aw-watcher-afk_PC/events": [event({"status": "not-afk"})],
            "/api/0/buckets/aw-watcher-web-chrome_PC/events": [],
            "/api/0/buckets/aw-watcher-web-edge_PC/events": [],
        }
        client, _ = self.client(replies)

        self.assertEqual(len(client.fetch_events(START, END)["window"]), 1)

    def test_windows_physical_dns_hostname_is_an_alternative_local_name(self):
        replies = {
            "/api/0/info": INFO,
            "/api/0/buckets/": BUCKETS,
            "/api/0/buckets/aw-watcher-window_PC/events": [event({"app": "Code.exe"})],
            "/api/0/buckets/aw-watcher-afk_PC/events": [event({"status": "not-afk"})],
            "/api/0/buckets/aw-watcher-web-chrome_PC/events": [],
            "/api/0/buckets/aw-watcher-web-edge_PC/events": [],
        }
        client, _ = self.client(replies)

        with (
            patch("aw_share.activitywatch.socket.gethostname", return_value="ALIAS"),
            patch(
                "aw_share.activitywatch._windows_physical_dns_hostname",
                return_value="pc.campus.example.",
            ),
        ):
            self.assertEqual(len(client.fetch_events(START, END)["window"]), 1)

    def test_missing_afk_or_empty_required_events_never_become_zero_usage(self):
        missing = {key: value for key, value in BUCKETS.items() if value["type"] != "afkstatus"}
        client, _ = self.client({"/api/0/info": INFO, "/api/0/buckets/": missing})
        with self.assertRaises(ActivityWatchError):
            client.fetch_events(START, END)

        client, _ = self.client(
            {
                "/api/0/info": INFO,
                "/api/0/buckets/": BUCKETS,
                "/api/0/buckets/aw-watcher-window_PC/events": [],
                "/api/0/buckets/aw-watcher-afk_PC/events": [event({"status": "not-afk"})],
            }
        )
        with self.assertRaisesRegex(ActivityWatchError, "unavailable"):
            client.fetch_events(START, END)

    def test_non_loopback_and_non_api_urls_are_rejected(self):
        unsafe = (
            "https://reports.example.test/api/0",
            "http://0.0.0.0:5600/api/0",
            "http://192.168.1.20:5600/api/0",
            "http://localhost.example.test:5600/api/0",
            "http://127.0.0.1:5600/not-the-api",
            "http://127.0.0.1:5600/api/0?next=https://reports.example.test",
        )
        for url in unsafe:
            with self.subTest(url=url), self.assertRaises(ActivityWatchError):
                ActivityWatchClient(url)
        self.assertEqual(
            ActivityWatchClient("http://localhost:5600/api/0").base_url,
            "http://127.0.0.1:5600/api/0",
        )

    def test_redirect_is_not_followed_or_reflected_in_errors(self):
        original = ActivityWatchClient()
        self.assertTrue(any(isinstance(handler, _NoRedirect) for handler in original._opener.handlers))
        redirect_handler = next(
            handler for handler in original._opener.handlers if isinstance(handler, _NoRedirect)
        )
        self.assertIsNone(
            redirect_handler.redirect_request(None, None, 302, "Found", {}, "https://outside.example/PRIVATE")
        )

        secret = "https://outside.example/PRIVATE_URL"
        redirected = HTTPError(
            "http://127.0.0.1:5600/api/0/info",
            302,
            "Found",
            {"Location": secret},
            io.BytesIO(f"SECRET_BODY {secret}".encode("utf-8")),
        )
        client, opener = self.client({"/api/0/info": redirected})
        with self.assertRaises(ActivityWatchError) as raised:
            client.get_info()

        self.assertEqual(len(opener.requests), 1)
        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn("SECRET_BODY", str(raised.exception))
        self.assertIn("redirect", str(raised.exception).lower())

    def test_http_and_invalid_json_errors_never_echo_private_response(self):
        secret = "https://study.example/SECRET_PATH?token=PRIVATE"
        denied = HTTPError(
            "http://127.0.0.1:5600/api/0/buckets/",
            401,
            "Unauthorized",
            {},
            io.BytesIO(json.dumps({"url": secret, "title": "SECRET_TITLE"}).encode()),
        )
        client, _ = self.client({"/api/0/buckets/": denied})
        with self.assertRaises(ActivityWatchError) as raised:
            client.get_buckets()
        self.assertIn("401", str(raised.exception))
        for sensitive in (secret, "SECRET_TITLE", "PRIVATE"):
            self.assertNotIn(sensitive, str(raised.exception))

        client, _ = self.client(
            {"/api/0/info": f'{{"title":"SECRET_TITLE","url":"{secret}"}} broken'.encode()}
        )
        with self.assertRaises(ActivityWatchError) as raised:
            client.get_info()
        self.assertIn("invalid JSON", str(raised.exception))
        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn("SECRET_TITLE", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
