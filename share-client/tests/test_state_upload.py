"""Consent persistence and transport safety using only synthetic snapshots."""

import json
import unittest
import uuid
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from aw_share.state import SharingState
from aw_share.upload import (
    ConsentChangedError,
    SummaryUploader,
    UploadError,
    prepare_report,
)


ZONE = timezone(timedelta(hours=8))
DAY = date(2026, 10, 5)
ENDPOINT = "https://reports.example.test/v1/summary"
OTHER_ENDPOINT = "https://another.example.test/v1/summary"


def local(hour, minute=0):
    return datetime(2026, 10, 5, hour, minute, tzinfo=ZONE)


def fresh_state(case):
    path = Path(__file__).resolve().parent / f".state-upload-{uuid.uuid4().hex}.json"
    case.addCleanup(path.unlink, missing_ok=True)
    case.addCleanup(Path(str(path) + ".lock").unlink, missing_ok=True)
    return SharingState.load(path)


def snapshot():
    return {
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


class SharingStateTests(unittest.TestCase):
    def test_default_disabled_and_pause_resume_does_not_backfill(self):
        state = fresh_state(self)
        self.assertFalse(state.enabled)
        self.assertFalse(state.path.exists())
        self.assertEqual(state.allowed_intervals(local(8), local(12), now=local(12)), [])

        state.enable(at=local(9), endpoint=ENDPOINT)
        state.pause(at=local(10))
        state.enable(at=local(11), endpoint=ENDPOINT)
        loaded = SharingState.load(state.path)
        self.assertTrue(loaded.enabled)
        self.assertEqual(loaded.consent_generation, 3)
        self.assertEqual(loaded.approved_endpoint, ENDPOINT)
        self.assertEqual(
            loaded.allowed_intervals(local(8), local(12), now=local(12)),
            [
                (local(9).astimezone(timezone.utc), local(10).astimezone(timezone.utc)),
                (local(11).astimezone(timezone.utc), local(12).astimezone(timezone.utc)),
            ],
        )
        stored = json.loads(state.path.read_text(encoding="utf-8"))
        self.assertTrue(stored["intervals"][0]["start_utc"].endswith("Z"))
        self.assertEqual(len(stored["intervals"]), 2)

        loaded.revoke(at=local(12))
        loaded.enable(at=local(13), endpoint=ENDPOINT)
        self.assertEqual(
            loaded.allowed_intervals(local(8), local(14), now=local(14)),
            [(local(13).astimezone(timezone.utc), local(14).astimezone(timezone.utc))],
        )

    def test_corrupt_state_fails_closed_without_recreating_consent(self):
        state = fresh_state(self)
        state.path.write_text("not json", encoding="utf-8")
        from aw_share.state import StateError

        with self.assertRaises(StateError):
            SharingState.load(state.path)
        self.assertEqual(state.path.read_text(encoding="utf-8"), "not json")

    def test_missing_interval_end_is_not_mistaken_for_open_consent(self):
        state = fresh_state(self)
        state.enable(at=local(9), endpoint=ENDPOINT)
        state.pause(at=local(10))
        raw = json.loads(state.path.read_text(encoding="utf-8"))
        del raw["intervals"][0]["end_utc"]
        state.path.write_text(json.dumps(raw), encoding="utf-8")
        from aw_share.state import StateError

        with self.assertRaises(StateError):
            SharingState.load(state.path)


class SummaryTransportTests(unittest.TestCase):
    def setUp(self):
        self.state = fresh_state(self)
        self.state.enable(at=local(9), endpoint=ENDPOINT)
        self.summary = snapshot()

    def test_endpoint_is_bound_to_user_consent(self):
        calls = []
        with self.assertRaises(ValueError):
            self.state.enable(at=local(10), endpoint=OTHER_ENDPOINT)
        uploader = SummaryUploader(
            OTHER_ENDPOINT,
            token="local-test-token",
            transport=lambda request, timeout: calls.append(request) or 200,
        )
        with self.assertRaises(ConsentChangedError):
            uploader.upload(self.summary, device_id=self.state.device_id, state=self.state)
        self.assertEqual(calls, [])

        self.state.pause(at=local(10))
        self.state.enable(at=local(11), endpoint=OTHER_ENDPOINT)
        self.assertEqual(
            self.state.allowed_intervals(local(8), local(12), now=local(12)),
            [(local(11).astimezone(timezone.utc), local(12).astimezone(timezone.utc))],
        )
        stale = deepcopy(self.summary)
        stale["data_cutoff"] = local(10).isoformat()
        stale["last_observed_at"] = local(10).isoformat()
        with self.assertRaises(ConsentChangedError):
            uploader.upload(stale, device_id=self.state.device_id, state=self.state)
        self.assertEqual(calls, [])
        uploader.upload(self.summary, device_id=self.state.device_id, state=self.state)
        self.assertEqual(len(calls), 1)

    def test_new_receiver_cannot_get_previous_days_daily_report(self):
        self.state.pause(at=local(10))
        tomorrow = datetime(2026, 10, 6, 11, tzinfo=ZONE)
        self.state.enable(at=tomorrow, endpoint=OTHER_ENDPOINT)
        self.assertEqual(
            self.state.allowed_intervals(local(0), local(23, 59), now=tomorrow), []
        )
        calls = []
        uploader = SummaryUploader(
            OTHER_ENDPOINT, token="local-test-token",
            transport=lambda request, timeout: calls.append(request) or 200,
        )
        with self.assertRaises(ConsentChangedError):
            uploader.upload(
                self.summary, device_id=self.state.device_id,
                state=self.state, kind="daily",
            )
        self.assertEqual(calls, [])

    def test_stale_snapshot_rejected_after_pause_and_resume(self):
        generation = self.state.consent_generation
        self.state.pause(at=local(10))
        self.state.enable(at=local(11), endpoint=ENDPOINT)
        calls = []
        uploader = SummaryUploader(
            ENDPOINT,
            token="local-test-token",
            transport=lambda request, timeout: calls.append(request) or 200,
        )
        with self.assertRaises(ConsentChangedError):
            uploader.upload(
                self.summary,
                device_id=self.state.device_id,
                state=self.state,
                expected_generation=generation,
            )
        self.assertEqual(calls, [])

    def test_report_cannot_exceed_authorized_duration(self):
        self.state.pause(at=local(10))
        self.state.enable(at=local(11), endpoint=ENDPOINT)
        bad = deepcopy(self.summary)
        bad["total_seconds"] = 7201
        calls = []
        uploader = SummaryUploader(
            ENDPOINT, token="local-test-token",
            transport=lambda request, timeout: calls.append(request) or 200,
        )
        with self.assertRaises(ConsentChangedError):
            uploader.upload(bad, device_id=self.state.device_id, state=self.state)
        self.assertEqual(calls, [])

    def test_pause_during_retry_cancels_next_request(self):
        calls = []

        def transport(request, timeout):
            calls.append(request)
            self.state.pause(at=local(10))
            return 503

        uploader = SummaryUploader(
            ENDPOINT, token="local-test-token", transport=transport,
            max_retries=2, retry_delay=0,
        )
        with self.assertRaises(ConsentChangedError):
            uploader.upload(
                self.summary, device_id=self.state.device_id, state=self.state,
                expected_generation=1,
            )
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.state.has_sent_day(DAY, "current"))

    def test_pause_while_server_returns_success_does_not_restore_receipt(self):
        requests = []

        def transport(request, timeout):
            requests.append(request)
            SharingState.load(self.state.path).pause(at=local(10))
            return 200

        uploader = SummaryUploader(
            ENDPOINT, token="local-test-token", transport=transport,
        )
        report_id = uploader.upload(
            self.summary, device_id=self.state.device_id, state=self.state,
            expected_generation=self.state.consent_generation,
        )
        self.assertEqual(requests[0].get_header("Idempotency-key"), report_id)
        reloaded = SharingState.load(self.state.path)
        self.assertFalse(reloaded.enabled)
        self.assertFalse(reloaded.has_sent_day(DAY, "current"))

    def test_nonaggregate_and_insecure_destinations_are_rejected(self):
        for field, value in (
            ("events", [{"url": "https://private.example/page"}]),
            ("applications", {r"C:\Users\private\Editor.exe": 3600}),
            ("applications", {"https://private.example/app.exe": 3600}),
            ("domains", {"https://private.example/page": 3600}),
            ("domains", {"private_host.example": 3600}),
        ):
            with self.subTest(field=field):
                bad = deepcopy(self.summary)
                bad[field] = value
                with self.assertRaises(ValueError) as error:
                    prepare_report(bad, device_id=self.state.device_id)
                self.assertNotIn("private", str(error.exception))
        with self.assertRaises(ValueError):
            self.state.enable(at=local(10), endpoint="http://reports.example.test/v1/summary")
        from aw_share.upload import ConfigurationError

        with self.assertRaises(ConfigurationError):
            SummaryUploader("http://reports.example.test/v1/summary", token="token")

    def test_legal_windows_application_name_with_percent_at_and_hash_uploads(self):
        allowed_name = "100%Game#@.exe"
        report = deepcopy(self.summary)
        report["applications"] = {allowed_name: 3600}
        report["category_applications"] = {"学习办公": {allowed_name: 3600}}
        captured = []
        uploader = SummaryUploader(
            ENDPOINT, token="local-test-token",
            transport=lambda request, timeout: captured.append(request) or 200,
        )
        uploader.upload(report, device_id=self.state.device_id, state=self.state)
        self.assertEqual(
            json.loads(captured[0].data)["summary"]["applications"],
            {allowed_name: 3600},
        )

    def test_international_hostname_is_valid_but_ip_address_is_not_domain(self):
        report = deepcopy(self.summary)
        report["domains"] = {"例子.中国": 300}
        report["category_domains"] = {"学习办公": {"例子.中国": 300}}
        _, body = prepare_report(report, device_id=self.state.device_id)
        self.assertEqual(json.loads(body)["summary"]["domains"], {"例子.中国": 300})
        report["domains"] = {"192.168.1.1": 300}
        report["category_domains"] = {"学习办公": {"192.168.1.1": 300}}
        with self.assertRaises(ValueError):
            prepare_report(report, device_id=self.state.device_id)

    def test_stale_watcher_coverage_is_kept_distinct_from_request_cutoff(self):
        stale = deepcopy(self.summary)
        stale["total_seconds"] = 300
        stale["applications"] = {"Editor.exe": 300}
        stale["categories"] = {"学习办公": 300}
        stale["category_applications"] = {"学习办公": {"Editor.exe": 300}}
        stale["activity_coverage"] = "partial"
        stale["unobserved_seconds"] = 3 * 3600 - 300
        stale["last_observed_at"] = local(9, 5).isoformat()
        report_id, body = prepare_report(stale, device_id=self.state.device_id)
        payload = json.loads(body)["summary"]
        self.assertEqual(payload["data_cutoff"], local(12).isoformat())
        self.assertEqual(payload["last_observed_at"], local(9, 5).isoformat())
        self.assertEqual(payload["activity_coverage"], "partial")
        self.assertEqual(payload["unobserved_seconds"], 10500)
        self.assertEqual(len(report_id), 64)

        calls = []
        uploader = SummaryUploader(
            ENDPOINT, token="local-test-token",
            transport=lambda request, timeout: calls.append(request) or 200,
        )
        self.assertEqual(
            uploader.upload(stale, device_id=self.state.device_id, state=self.state),
            report_id,
        )
        self.assertEqual(json.loads(calls[0].data)["summary"]["last_observed_at"], local(9, 5).isoformat())

        original_id = prepare_report(self.summary, device_id=self.state.device_id)[0]
        self.assertNotEqual(report_id, original_id)
        invalid = deepcopy(stale)
        invalid["last_observed_at"] = local(13).isoformat()
        with self.assertRaises(ValueError):
            prepare_report(invalid, device_id=self.state.device_id)
        invalid = deepcopy(stale)
        invalid["activity_coverage"] = "complete"
        with self.assertRaises(ValueError):
            prepare_report(invalid, device_id=self.state.device_id)

    def test_content_changes_get_new_id_and_only_success_is_recorded(self):
        first_id, first_body = prepare_report(self.summary, device_id=self.state.device_id)
        self.assertEqual(first_id, prepare_report(self.summary, device_id=self.state.device_id)[0])
        updated = deepcopy(self.summary)
        updated["data_cutoff"] = local(12, 15).isoformat()
        second_id, second_body = prepare_report(updated, device_id=self.state.device_id)
        self.assertNotEqual(first_id, second_id)
        self.assertNotEqual(first_body, second_body)

        failures = []
        unavailable = SummaryUploader(
            ENDPOINT, token="local-test-token",
            transport=lambda request, timeout: failures.append(request) or 503,
            max_retries=1, retry_delay=0,
        )
        with self.assertRaises(UploadError):
            unavailable.upload(self.summary, device_id=self.state.device_id, state=self.state)
        self.assertEqual(len(failures), 2)
        self.assertEqual(failures[0].data, failures[1].data)
        self.assertEqual(failures[0].get_header("Idempotency-key"), first_id)
        self.assertFalse(SharingState.load(self.state.path).has_sent_day(DAY, "current"))

        calls = []
        available = SummaryUploader(
            ENDPOINT, token="local-test-token",
            transport=lambda request, timeout: calls.append(request) or 200,
        )
        self.assertEqual(available.upload(self.summary, device_id=self.state.device_id, state=self.state), first_id)
        self.assertEqual(available.upload(self.summary, device_id=self.state.device_id, state=self.state), first_id)
        self.assertEqual(len(calls), 1)
        self.assertTrue(SharingState.load(self.state.path).has_sent_day(DAY, "current"))
        self.assertEqual(available.upload(updated, device_id=self.state.device_id, state=self.state), second_id)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("local-test-token", self.state.path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

