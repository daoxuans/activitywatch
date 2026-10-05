"""Behavioral tests for consent-bounded ActivityWatch daily summaries.

These fixtures deliberately include data that must never leave the device.
Only aggregated application names, domains, and category durations may appear
in the result of ``summarize_day``.
"""

import json
import unittest
from datetime import date, datetime, timedelta, timezone

from aw_share.aggregate import DataUnavailable, summarize_day


CHINA_TIME = timezone(timedelta(hours=8))
DAY = date(2026, 10, 5)
NEXT_DAY = date(2026, 10, 6)
NOW = datetime(2026, 10, 6, 0, 30, tzinfo=CHINA_TIME)


def local(hour, minute=0, day=DAY):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=CHINA_TIME)


def event(start, duration_seconds, data, **extra):
    timestamp = start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "timestamp": timestamp,
        "duration": duration_seconds,
        "data": data,
        **extra,
    }


def window(start, duration_seconds, app, title="local-only window title"):
    return event(start, duration_seconds, {"app": app, "title": title})


def afk(start, duration_seconds, status):
    return event(start, duration_seconds, {"status": status})


def web(start, duration_seconds, url, bucket_id="aw-watcher-web-chrome"):
    return event(
        start,
        duration_seconds,
        {"url": url, "title": "local-only webpage title"},
        bucket_id=bucket_id,
    )


def summary(
    windows,
    afks,
    webs=(),
    *,
    day=DAY,
    now=NOW,
    allowed_intervals=None,
    app_categories=None,
    domain_categories=None,
    browser_apps=None,
):
    if allowed_intervals is None:
        allowed_intervals = [(local(0, day=day), local(0, day=day + timedelta(days=1)))]
    return summarize_day(
        day=day,
        timezone=CHINA_TIME,
        now=now,
        window_events=windows,
        afk_events=afks,
        web_events=webs,
        allowed_intervals=allowed_intervals,
        app_categories=app_categories,
        domain_categories=domain_categories,
        browser_apps=browser_apps,
    )


class SummarizeDayTests(unittest.TestCase):
    def test_counts_only_foreground_time_while_not_afk(self):
        result = summary(
            [window(local(9), 30 * 60, "Editor.exe")],
            [
                afk(local(9), 10 * 60, "not-afk"),
                afk(local(9, 10), 10 * 60, "afk"),
                afk(local(9, 20), 10 * 60, "not-afk"),
            ],
            app_categories={"Editor.exe": "学习办公"},
        )

        self.assertEqual(result["total_seconds"], 20 * 60)
        self.assertEqual(result["applications"], {"Editor.exe": 20 * 60})
        self.assertEqual(result["categories"], {"学习办公": 20 * 60})

    def test_clips_one_event_at_both_daily_boundaries(self):
        windows = [window(local(23, 55), 10 * 60, "Editor.exe")]
        afks = [afk(local(23, 55), 10 * 60, "not-afk")]
        consent = [(local(23, 55), local(0, 5, NEXT_DAY))]

        first = summary(windows, afks, allowed_intervals=consent)
        second = summary(
            windows, afks, day=NEXT_DAY, allowed_intervals=consent
        )

        for result in (first, second):
            self.assertEqual(result["total_seconds"], 5 * 60)
            self.assertEqual(result["applications"], {"Editor.exe": 5 * 60})

    def test_domain_is_a_browser_drilldown_not_additional_total_time(self):
        result = summary(
            [window(local(10), 20 * 60, "chrome.exe")],
            [afk(local(10), 20 * 60, "not-afk")],
            [web(local(10, 5), 10 * 60, "https://video.example/watch?v=local-only")],
            app_categories={"chrome.exe": "其他"},
            domain_categories={"video.example": "影音"},
            browser_apps={"chrome.exe"},
        )

        self.assertEqual(result["total_seconds"], 20 * 60)
        self.assertEqual(result["applications"], {"chrome.exe": 20 * 60})
        self.assertEqual(result["domains"], {"video.example": 10 * 60})
        self.assertEqual(
            result["categories"], {"网页未覆盖": 10 * 60, "影音": 10 * 60}
        )
        self.assertEqual(result["web_uncovered_seconds"], 10 * 60)
        self.assertEqual(sum(result["categories"].values()), result["total_seconds"])

    def test_background_browser_tab_is_not_counted_as_site_time(self):
        result = summary(
            [
                window(local(10), 10 * 60, "Editor.exe"),
                window(local(10, 10), 10 * 60, "chrome.exe"),
            ],
            [afk(local(10), 20 * 60, "not-afk")],
            [web(local(10), 10 * 60, "https://background.example/page")],
            browser_apps={"chrome.exe"},
        )

        self.assertEqual(result["total_seconds"], 20 * 60)
        self.assertNotIn("background.example", result["domains"])
        self.assertEqual(result["web_uncovered_seconds"], 10 * 60)

    def test_browser_source_uses_watcher_name_not_host_name(self):
        result = summary(
            [window(local(10), 10 * 60, "msedge.exe")],
            [afk(local(10), 10 * 60, "not-afk")],
            [
                web(
                    local(10),
                    10 * 60,
                    "https://private.example/path",
                    bucket_id="aw-watcher-web-chrome_MyEdgeLaptop",
                )
            ],
        )

        self.assertEqual(result["domains"], {})
        self.assertEqual(result["web_uncovered_seconds"], 10 * 60)

    def test_unknown_browser_source_is_uncovered_even_when_it_is_the_only_bucket(self):
        result = summary(
            [window(local(10), 10 * 60, "chrome.exe")],
            [afk(local(10), 10 * 60, "not-afk")],
            [web(local(10), 10 * 60, "https://private.example", bucket_id="unknown")],
        )

        self.assertEqual(result["domains"], {})
        self.assertEqual(result["web_uncovered_seconds"], 10 * 60)

    def test_vivaldi_is_treated_as_a_browser_and_its_domain_is_attributed(self):
        result = summary(
            [window(local(10), 10 * 60, "vivaldi.exe")],
            [afk(local(10), 10 * 60, "not-afk")],
            [
                web(
                    local(10), 10 * 60, "https://study.example/lesson",
                    bucket_id="aw-watcher-web-vivaldi_StudentPC",
                )
            ],
        )

        self.assertEqual(result["domains"], {"study.example": 10 * 60})
        self.assertEqual(result["web_uncovered_seconds"], 0)

    def test_custom_browser_mapping_can_attribute_an_unknown_exe(self):
        result = summary(
            [window(local(10), 5 * 60, "StudyBrowser.exe")],
            [afk(local(10), 5 * 60, "not-afk")],
            [
                web(
                    local(10), 5 * 60, "https://study.example/lesson",
                    bucket_id="aw-watcher-web-studybrowser_PC",
                )
            ],
            browser_apps={"studybrowser": ["StudyBrowser.exe"]},
        )

        self.assertEqual(result["domains"], {"study.example": 5 * 60})

    def test_legacy_incognito_strings_are_interpreted_as_booleans(self):
        public = web(local(10), 5 * 60, "https://public.example")
        public["data"]["incognito"] = "false"
        private = web(local(10, 5), 5 * 60, "https://private.example")
        private["data"]["incognito"] = "true"
        report = summary(
            [window(local(10), 10 * 60, "chrome.exe")],
            [afk(local(10), 10 * 60, "not-afk")],
            [public, private],
            allowed_intervals=[(local(10), local(10, 10))],
        )

        self.assertEqual(report["domains"], {"public.example": 5 * 60})
        self.assertNotIn("private.example", report["domains"])
        self.assertEqual(report["web_uncovered_seconds"], 5 * 60)

    def test_invalid_host_is_uncovered_instead_of_poisoning_the_report(self):
        result = summary(
            [window(local(10), 10 * 60, "chrome.exe")],
            [afk(local(10), 10 * 60, "not-afk")],
            [web(local(10), 10 * 60, "https://bad host.example/private")],
            allowed_intervals=[(local(10), local(10, 10))],
        )

        self.assertEqual(result["domains"], {})
        self.assertEqual(result["web_uncovered_seconds"], 10 * 60)

    def test_stale_watchers_mark_activity_and_website_coverage_partial(self):
        report = summary(
            [window(local(9), 5 * 60, "chrome.exe")],
            [afk(local(9), 5 * 60, "not-afk")],
            now=local(20),
            allowed_intervals=[(local(9), local(20))],
        )

        self.assertEqual(report["total_seconds"], 5 * 60)
        self.assertEqual(report["data_cutoff"], local(20).isoformat())
        self.assertEqual(report["last_observed_at"], local(9, 5).isoformat())
        self.assertEqual(report["activity_coverage"], "partial")
        self.assertEqual(report["website_coverage"], "partial")
        self.assertEqual(report["unobserved_seconds"], 10 * 3600 + 55 * 60)

    def test_fully_observed_authorized_interval_is_complete(self):
        report = summary(
            [window(local(9), 60 * 60, "Editor.exe")],
            [afk(local(9), 60 * 60, "not-afk")],
            now=local(10),
            allowed_intervals=[(local(9), local(10))],
        )

        self.assertEqual(report["activity_coverage"], "complete")
        self.assertEqual(report["website_coverage"], "complete")
        self.assertEqual(report["unobserved_seconds"], 0)
        self.assertEqual(report["last_observed_at"], local(10).isoformat())

    def test_disjoint_required_watcher_events_are_unavailable_not_zero(self):
        with self.assertRaises(DataUnavailable):
            summary(
                [window(local(9), 5 * 60, "Editor.exe")],
                [afk(local(10), 5 * 60, "not-afk")],
                allowed_intervals=[(local(9), local(11))],
            )

    def test_missing_web_events_leave_browser_time_uncovered(self):
        result = summary(
            [window(local(10), 20 * 60, "chrome.exe")],
            [afk(local(10), 20 * 60, "not-afk")],
            browser_apps={"chrome.exe"},
        )

        self.assertEqual(result["applications"], {"chrome.exe": 20 * 60})
        self.assertEqual(result["domains"], {})
        self.assertEqual(result["web_uncovered_seconds"], 20 * 60)

    def test_result_never_contains_titles_url_details_or_executable_path(self):
        result = summary(
            [
                window(
                    local(9),
                    10 * 60,
                    r"C:\Users\Alice\Private\ExamApp.exe",
                    title="TOP_SECRET_WINDOW_TITLE",
                ),
                window(local(9, 10), 10 * 60, "chrome.exe"),
            ],
            [afk(local(9), 20 * 60, "not-afk")],
            [
                web(
                    local(9, 10),
                    10 * 60,
                    "https://alice:password@study.example/course/lesson"
                    "?secret=TOP_SECRET_QUERY#TOP_SECRET_FRAGMENT",
                )
            ],
            browser_apps={"chrome.exe"},
        )

        self.assertEqual(result["applications"]["ExamApp.exe"], 10 * 60)
        self.assertEqual(result["domains"], {"study.example": 10 * 60})
        payload = json.dumps(result, ensure_ascii=False)
        for forbidden in (
            "Alice",
            "Private",
            "TOP_SECRET_WINDOW_TITLE",
            "local-only webpage title",
            "password",
            "/course/lesson",
            "TOP_SECRET_QUERY",
            "TOP_SECRET_FRAGMENT",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, payload)

    def test_resume_does_not_backfill_activity_from_paused_interval(self):
        consent = [
            (local(9), local(10)),
            (local(11), local(12)),
        ]
        result = summary(
            [
                window(local(9), 60 * 60, "Editor.exe"),
                window(local(10), 60 * 60, "chrome.exe"),
                window(local(11), 60 * 60, "Editor.exe"),
            ],
            [afk(local(9), 3 * 60 * 60, "not-afk")],
            [web(local(10), 60 * 60, "https://paused.example/private")],
            allowed_intervals=consent,
            browser_apps={"chrome.exe"},
        )

        self.assertEqual(result["total_seconds"], 2 * 60 * 60)
        self.assertEqual(result["applications"], {"Editor.exe": 2 * 60 * 60})
        self.assertNotIn("paused.example", result["domains"])
        self.assertEqual(result["web_uncovered_seconds"], 0)

    def test_missing_window_watcher_reports_unavailable_not_zero(self):
        with self.assertRaises(DataUnavailable):
            summary([], [afk(local(9), 60, "not-afk")])

    def test_missing_afk_watcher_reports_unavailable_not_zero(self):
        with self.assertRaises(DataUnavailable):
            summary([window(local(9), 60, "Editor.exe")], [])


if __name__ == "__main__":
    unittest.main()
