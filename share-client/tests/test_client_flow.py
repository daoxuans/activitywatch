"""Local, synthetic end-to-end checks; no real ActivityWatch or cloud calls."""

import json
import unittest
import uuid
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from aw_share.client import run_once
from aw_share.state import SharingState
from aw_share.upload import AuthenticationError, SummaryUploader


CHINA_TIME = timezone(timedelta(hours=8))
DAY = date(2026, 10, 5)
NEXT_DAY = date(2026, 10, 6)
ENDPOINT = "https://reports.example.test/v1/summary"


def local(hour, minute=0, day=DAY):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=CHINA_TIME)


def event(start, duration, data, **extra):
    return {
        "timestamp": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "duration": duration,
        "data": data,
        **extra,
    }


def window(start, duration, app, title="LOCAL_ONLY_WINDOW_TITLE"):
    return event(start, duration, {"app": app, "title": title})


def afk(start, duration):
    return event(start, duration, {"status": "not-afk"})


def web(start, duration, url):
    return event(
        start,
        duration,
        {"url": url, "title": "LOCAL_ONLY_WEB_TITLE"},
        bucket_id="aw-watcher-web-chrome",
    )


class FakeActivityWatch:
    def __init__(self, events, on_fetch=None):
        self.events = events
        self.on_fetch = on_fetch
        self.calls = []

    def fetch_events(self, start, end):
        self.calls.append((start, end))
        if self.on_fetch is not None:
            self.on_fetch()
        return deepcopy(self.events)


class FakeUploader:
    def __init__(self):
        self.calls = []

    def upload(self, summary, device_id, state, **kwargs):
        self.calls.append((deepcopy(summary), device_id, state, kwargs))
        return "mock-report-id"


def fresh_state(test_case):
    """Give each case its own state files in the writable test directory."""
    folder = Path(__file__).resolve().parent
    state_path = folder / f".sharing-test-{uuid.uuid4().hex}.json"
    lock_path = Path(str(state_path) + ".lock")
    test_case.addCleanup(state_path.unlink, missing_ok=True)
    test_case.addCleanup(lock_path.unlink, missing_ok=True)
    return SharingState.load(state_path)


class ClientFlowTests(unittest.TestCase):
    def setUp(self):
        self.state = fresh_state(self)

    def run_client(self, aw_client, uploader, *, day=DAY, now=None, upload=True):
        return run_once(
            day=day,
            now=now or local(12),
            timezone=CHINA_TIME,
            aw_client=aw_client,
            state=self.state,
            uploader=uploader,
            upload=upload,
        )

    def test_disabled_client_does_not_even_read_local_events(self):
        aw_client = FakeActivityWatch({"window": [], "afk": [], "web": []})
        uploader = FakeUploader()

        self.assertIsNone(self.run_client(aw_client, uploader))
        self.assertEqual(aw_client.calls, [])
        self.assertEqual(uploader.calls, [])

    def test_pause_gap_is_not_backfilled_after_sharing_resumes(self):
        self.state.enable(at=local(9), endpoint=ENDPOINT)
        self.state.pause(at=local(10))
        self.state.enable(at=local(11), endpoint=ENDPOINT)
        aw_client = FakeActivityWatch(
            {
                "window": [
                    window(local(9), 3600, "Editor.exe"),
                    window(local(10), 3600, "chrome.exe"),
                    window(local(11), 3600, "Editor.exe"),
                ],
                "afk": [afk(local(9), 3 * 3600)],
                "web": [web(local(10), 3600, "https://paused.example/private")],
            }
        )
        uploader = FakeUploader()

        report = self.run_client(aw_client, uploader)

        self.assertEqual(report["total_seconds"], 2 * 3600)
        self.assertEqual(report["applications"], {"Editor.exe": 2 * 3600})
        self.assertEqual(report["domains"], {})
        self.assertEqual(len(uploader.calls), 1)
        payload = json.dumps(uploader.calls[0][0], ensure_ascii=False)
        self.assertNotIn("paused.example", payload)
        self.assertNotIn("chrome.exe", payload)

    def test_paused_client_does_not_read_or_upload(self):
        self.state.enable(at=local(9), endpoint=ENDPOINT)
        self.state.pause(at=local(10))
        aw_client = FakeActivityWatch({"window": [], "afk": [], "web": []})
        uploader = FakeUploader()

        self.assertIsNone(self.run_client(aw_client, uploader))
        self.assertEqual(aw_client.calls, [])
        self.assertEqual(uploader.calls, [])

    def test_revoke_during_local_read_prevents_upload(self):
        self.state.enable(at=local(9), endpoint=ENDPOINT)
        aw_client = FakeActivityWatch(
            {
                "window": [window(local(9), 3600, "Editor.exe")],
                "afk": [afk(local(9), 3600)],
                "web": [],
            },
            # Simulate the visible controls running in another process while
            # the ActivityWatch read is in progress. The client must refresh.
            on_fetch=lambda: SharingState.load(self.state.path).revoke(at=local(10)),
        )
        uploader = FakeUploader()

        self.assertIsNone(self.run_client(aw_client, uploader))
        self.assertEqual(uploader.calls, [])

    def test_cross_midnight_query_includes_previous_day_start(self):
        self.state.enable(at=local(23, 55), endpoint=ENDPOINT)
        aw_client = FakeActivityWatch(
            {
                "window": [window(local(23, 55), 600, "Editor.exe")],
                "afk": [afk(local(23, 55), 600)],
                "web": [],
            }
        )
        uploader = FakeUploader()

        report = self.run_client(
            aw_client, uploader, day=NEXT_DAY, now=local(0, 30, NEXT_DAY)
        )

        self.assertEqual(report["total_seconds"], 300)
        self.assertEqual(report["applications"], {"Editor.exe": 300})
        self.assertEqual(len(aw_client.calls), 1)
        query_start, query_end = aw_client.calls[0]
        self.assertEqual(query_start, local(0))
        self.assertEqual(query_end, local(0, 30, NEXT_DAY))

    def test_report_preview_does_not_send_anything(self):
        self.state.enable(at=local(9), endpoint=ENDPOINT)
        aw_client = FakeActivityWatch(
            {
                "window": [window(local(9), 60, "Editor.exe")],
                "afk": [afk(local(9), 60)],
                "web": [],
            }
        )
        uploader = FakeUploader()

        report = self.run_client(aw_client, uploader, upload=False)

        self.assertEqual(report["total_seconds"], 60)
        self.assertEqual(uploader.calls, [])

    def test_utc_now_still_uses_the_configured_local_report_day(self):
        self.state.enable(at=local(9), endpoint=ENDPOINT)
        aw_client = FakeActivityWatch(
            {
                "window": [window(local(9), 60, "Editor.exe")],
                "afk": [afk(local(9), 60)],
                "web": [],
            }
        )

        report = self.run_client(
            aw_client, FakeUploader(), now=local(12).astimezone(timezone.utc), upload=False
        )

        self.assertEqual(report["data_cutoff"], local(12).isoformat())

    def test_wire_payload_contains_no_titles_full_url_paths_or_timeline(self):
        self.state.enable(at=local(9), endpoint=ENDPOINT)
        aw_client = FakeActivityWatch(
            {
                "window": [
                    window(
                        local(9),
                        600,
                        r"C:\Users\Alice\Private\ExamApp.exe",
                        "TOP_SECRET_WINDOW_TITLE",
                    ),
                    window(local(9, 10), 600, "chrome.exe"),
                ],
                "afk": [afk(local(9), 1200)],
                "web": [
                    web(
                        local(9, 10),
                        600,
                        "https://alice:password@study.example/private/course"
                        "?secret=TOP_SECRET_QUERY#TOP_SECRET_FRAGMENT",
                    )
                ],
            }
        )
        captured_requests = []

        def mock_https_transport(request, timeout):
            captured_requests.append(request)
            return 200

        uploader = SummaryUploader(
            ENDPOINT,
            token="local-test-token",
            transport=mock_https_transport,
            retry_delay=0,
        )

        report = self.run_client(aw_client, uploader, now=local(9, 20))

        self.assertEqual(report["applications"]["ExamApp.exe"], 600)
        self.assertEqual(report["domains"], {"study.example": 600})
        self.assertEqual(len(captured_requests), 1)
        wire = captured_requests[0].data.decode("utf-8")
        parsed = json.loads(wire)
        self.assertIsInstance(parsed, dict)
        for forbidden in (
            "Alice",
            "Private",
            "TOP_SECRET_WINDOW_TITLE",
            "LOCAL_ONLY_WEB_TITLE",
            "password",
            "/private/course",
            "TOP_SECRET_QUERY",
            "TOP_SECRET_FRAGMENT",
            '"timestamp"',
            '"duration"',
            '"data"',
            '"events"',
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, wire)


class SummaryUploaderTests(unittest.TestCase):
    def setUp(self):
        self.state = fresh_state(self)
        self.state.enable(at=local(9), endpoint=ENDPOINT)
        self.report = {
            "day": DAY.isoformat(),
            "data_cutoff": local(12).isoformat(),
            "authorized": True,
            "total_seconds": 3600,
            "applications": {"Editor.exe": 3600},
            "domains": {},
            "categories": {"学习办公": 3600},
            "category_applications": {"学习办公": {"Editor.exe": 3600}},
            "category_domains": {},
            "web_uncovered_seconds": 0,
            "website_coverage": "complete",
            "activity_coverage": "complete",
            "unobserved_seconds": 0,
            "last_observed_at": local(12).isoformat(),
        }

    def test_same_snapshot_retries_with_same_id_and_is_not_resent_after_ack(self):
        requests = []
        statuses = iter((503, 200))

        def mock_https_transport(request, timeout):
            requests.append(request)
            return next(statuses)

        uploader = SummaryUploader(
            ENDPOINT,
            token="local-test-token",
            transport=mock_https_transport,
            max_retries=1,
            retry_delay=0,
        )

        first_id = uploader.upload(
            self.report,
            device_id=self.state.device_id,
            state=self.state,
            expected_generation=self.state.consent_generation,
        )
        second_id = uploader.upload(
            self.report,
            device_id=self.state.device_id,
            state=self.state,
            expected_generation=self.state.consent_generation,
        )

        self.assertEqual(first_id, second_id)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].data, requests[1].data)
        self.assertEqual(
            requests[0].get_header("Idempotency-key"),
            requests[1].get_header("Idempotency-key"),
        )
        self.assertTrue(requests[0].get_header("Idempotency-key"))

    def test_authentication_failure_is_not_retried_or_logged_with_secrets(self):
        requests = []

        def mock_https_transport(request, timeout):
            requests.append(request)
            return 401

        uploader = SummaryUploader(
            ENDPOINT,
            token="TOP_SECRET_TOKEN",
            transport=mock_https_transport,
            max_retries=3,
            retry_delay=0,
        )

        with self.assertRaises(AuthenticationError) as raised:
            uploader.upload(
                self.report,
                device_id=self.state.device_id,
                state=self.state,
                expected_generation=self.state.consent_generation,
            )

        self.assertEqual(len(requests), 1)
        self.assertNotIn("TOP_SECRET_TOKEN", str(raised.exception))
        self.assertNotIn("reports.example.test", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
