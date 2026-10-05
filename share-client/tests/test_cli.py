"""Exercise visible opt-in controls without a live watcher or network."""

import io
import json
import os
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from aw_share import cli
from aw_share.state import SharingState


CHINA_TIME = timezone(timedelta(hours=8))
ENDPOINT = "https://reports.example.test/v1/summary"


def local(hour, minute=0):
    return datetime(2026, 10, 5, hour, minute, tzinfo=CHINA_TIME)


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        fixed = local(12)
        return fixed.astimezone(tz) if tz is not None else fixed.replace(tzinfo=None)


def fixture_path(test_case, suffix):
    """Use one exact, removable file under tests; no TemporaryDirectory ACLs."""
    folder = Path(__file__).resolve().parent
    path = folder / f".cli-test-{uuid.uuid4().hex}{suffix}"
    test_case.addCleanup(path.unlink, missing_ok=True)
    if suffix == ".json":
        test_case.addCleanup(Path(str(path) + ".lock").unlink, missing_ok=True)
    return path


def invoke(argv):
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = cli.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


def invoke_cp1252(argv):
    """Simulate Windows output redirected through a legacy-codepage pipe."""
    stdout_bytes, stderr_bytes = io.BytesIO(), io.BytesIO()
    stdout = io.TextIOWrapper(stdout_bytes, encoding="cp1252")
    stderr = io.TextIOWrapper(stderr_bytes, encoding="cp1252")
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            code = cli.main(argv)
        except SystemExit as exc:
            code = exc.code
    stdout.flush()
    stderr.flush()
    return code, stdout_bytes.getvalue().decode("utf-8"), stderr_bytes.getvalue().decode("utf-8")


def aw_event(start, seconds, data, **extra):
    return {
        "timestamp": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "duration": seconds,
        "data": data,
        **extra,
    }


class FakeActivityWatch:
    def __init__(self):
        self.calls = []

    def fetch_events(self, start, end):
        self.calls.append((start, end))
        return {
            "window": [
                aw_event(local(9), 3600, {"app": "Editor.exe", "title": "LOCAL_ONLY"}),
                aw_event(local(10), 3600, {"app": "chrome.exe", "title": "LOCAL_ONLY"}),
            ],
            "afk": [aw_event(local(9), 7200, {"status": "not-afk"})],
            "web": [
                aw_event(
                    local(10),
                    3600,
                    {"url": "https://sub.study.example/private?token=LOCAL_ONLY"},
                    bucket_id="aw-watcher-web-chrome",
                )
            ],
        }


class FakeUploader:
    def __init__(self):
        self.calls = []

    def upload(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return "fake-receipt"


class CliTests(unittest.TestCase):
    def setUp(self):
        self.state_file = fixture_path(self, ".json")
        self.base_args = ["--state-file", str(self.state_file)]

    def test_help_status_and_errors_work_with_cp1252_output(self):
        cases = (
            ([*self.base_args, "--help"], 0, "仅在明确启用", ""),
            ([*self.base_args, "status"], 0, "分享状态：", ""),
            (
                [*self.base_args, "--utc-offset", "bad", "status"],
                1,
                "",
                "操作未完成：时区偏移",
            ),
        )
        for argv, expected_code, stdout_text, stderr_text in cases:
            with self.subTest(argv=argv):
                code, output, error = invoke_cp1252(argv)
                self.assertEqual(code, expected_code)
                if stdout_text:
                    self.assertIn(stdout_text, output)
                else:
                    self.assertEqual(output, "")
                if stderr_text:
                    self.assertIn(stderr_text, error)
                else:
                    self.assertEqual(error, "")

    def test_status_is_disabled_by_default_and_does_not_contact_watchers(self):
        with patch.object(cli, "_aw_client", side_effect=AssertionError("AW contacted")), patch.object(
            cli, "_uploader", side_effect=AssertionError("uploader contacted")
        ):
            code, output, error = invoke([*self.base_args, "status"])

        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertIn("已暂停/未开启", output)
        self.assertIn("已确认的接收地址：无", output)
        self.assertFalse(self.state_file.exists())

    def test_enable_requires_exact_visible_confirmation_and_binds_endpoint(self):
        argv = [*self.base_args, "--endpoint", ENDPOINT, "enable"]
        with patch.object(cli, "datetime", FixedDateTime), patch(
            "builtins.input", return_value="同意"
        ):
            code, output, error = invoke(argv)
        self.assertEqual(code, 1)
        self.assertEqual(error, "")
        self.assertIn("未开启", output)
        self.assertFalse(SharingState.load(self.state_file).enabled)

        with patch.object(cli, "datetime", FixedDateTime), patch(
            "builtins.input", return_value="我同意分享"
        ):
            code, output, error = invoke(argv)
        state = SharingState.load(self.state_file)
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertTrue(state.enabled)
        self.assertEqual(state.approved_endpoint, ENDPOINT)
        self.assertIn("软件名称", output)
        self.assertIn("网站域名", output)
        self.assertIn(ENDPOINT, output)

    def test_invalid_http_endpoint_is_rejected_before_consent_prompt(self):
        with patch("builtins.input", side_effect=AssertionError("prompted for unsafe URL")):
            code, output, error = invoke(
                [*self.base_args, "--endpoint", "http://example.test/report", "enable"]
            )

        self.assertEqual(code, 1)
        self.assertIn("HTTPS", error)
        self.assertFalse(SharingState.load(self.state_file).enabled)

    def test_enable_without_interactive_input_fails_closed(self):
        with patch("builtins.input", side_effect=EOFError):
            code, output, error = invoke(
                [*self.base_args, "--endpoint", ENDPOINT, "enable"]
            )

        self.assertEqual(code, 1)
        self.assertEqual(error, "")
        self.assertIn("未开启", output)
        self.assertFalse(SharingState.load(self.state_file).enabled)

    def test_local_preview_needs_fresh_confirmation_but_no_cloud_config(self):
        fake_aw = FakeActivityWatch()
        with patch.object(cli, "datetime", FixedDateTime), patch(
            "builtins.input", return_value="不确认"
        ), patch.object(cli, "_aw_client", side_effect=AssertionError("AW contacted")):
            denied, output, error = invoke([*self.base_args, "local-preview"])
        self.assertEqual(denied, 1)
        self.assertEqual(error, "")
        self.assertIn("未读取", output)

        with patch.object(cli, "datetime", FixedDateTime), patch(
            "builtins.input", return_value="我同意本机预览"
        ), patch.object(cli, "_aw_client", return_value=fake_aw), patch.object(
            cli, "_uploader", side_effect=AssertionError("uploaded")
        ):
            code, output, error = invoke([*self.base_args, "local-preview"])

        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        report = json.loads(output[output.index("{") :])
        self.assertEqual(report["total_seconds"], 7200)
        self.assertFalse(report["authorized"])
        self.assertTrue(report["local_only"])
        self.assertEqual(len(fake_aw.calls), 1)
        self.assertFalse(self.state_file.exists())

    def test_explicit_missing_categories_file_is_not_silently_ignored(self):
        SharingState.load(self.state_file).enable(at=local(9), endpoint=ENDPOINT)
        with patch.object(cli, "datetime", FixedDateTime):
            code, _, error = invoke(
                [*self.base_args, "--categories", str(self.state_file.parent / "not-found.rules"), "preview"]
            )

        self.assertEqual(code, 1)
        self.assertIn("分类文件不存在", error)

    def test_missing_token_fails_send_and_watch_before_reading_activity(self):
        SharingState.load(self.state_file).enable(at=local(9), endpoint=ENDPOINT)
        fake_aw = FakeActivityWatch()
        with patch.object(cli, "datetime", FixedDateTime), patch.dict(
            os.environ, {"AW_SHARE_API_TOKEN": ""}
        ), patch.object(cli, "_aw_client", return_value=fake_aw):
            send_code, _, send_error = invoke([*self.base_args, "send"])
            watch_code, _, watch_error = invoke([*self.base_args, "watch"])

        self.assertEqual(send_code, 1)
        self.assertEqual(watch_code, 1)
        self.assertIn("AW_SHARE_API_TOKEN", send_error)
        self.assertIn("AW_SHARE_API_TOKEN", watch_error)
        self.assertEqual(fake_aw.calls, [])

    def test_pause_or_revoke_keeps_send_and_watch_from_reading_or_sending(self):
        with patch.object(cli, "datetime", FixedDateTime):
            for action in ("pause", "revoke"):
                with self.subTest(action=action):
                    path = fixture_path(self, ".json")
                    SharingState.load(path).enable(at=local(9), endpoint=ENDPOINT)
                    args = ["--state-file", str(path), "--endpoint", ENDPOINT]
                    code, _, error = invoke([*args, action])
                    self.assertEqual(code, 0)
                    self.assertEqual(error, "")

                    fake_uploader = FakeUploader()
                    with patch.object(
                        cli, "_aw_client", side_effect=AssertionError("AW contacted")
                    ), patch.object(cli, "_uploader", return_value=fake_uploader), patch.object(
                        cli.time, "sleep", side_effect=AssertionError("watch did not stop")
                    ):
                        send_code, send_output, _ = invoke([*args, "send"])
                        watch_code, watch_output, _ = invoke([*args, "watch"])

                    self.assertEqual(send_code, 0)
                    self.assertEqual(watch_code, 0)
                    self.assertIn("未读取和发送", send_output)
                    self.assertIn("分享已暂停", watch_output)
                    self.assertEqual(fake_uploader.calls, [])

    def test_category_file_reaches_preview_without_an_upload(self):
        categories = fixture_path(self, ".rules")
        categories.write_text(
            json.dumps(
                {
                    "apps": {"Editor.exe": "学习办公"},
                    "domains": {"study.example": "学习网站"},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        SharingState.load(self.state_file).enable(at=local(9), endpoint=ENDPOINT)
        fake_aw = FakeActivityWatch()
        with patch.object(cli, "datetime", FixedDateTime), patch.object(
            cli, "_aw_client", return_value=fake_aw
        ), patch.object(cli, "_uploader", side_effect=AssertionError("preview uploaded")):
            code, output, error = invoke(
                [*self.base_args, "--categories", str(categories), "preview"]
            )

        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        report = json.loads(output)
        self.assertEqual(report["total_seconds"], 7200)
        self.assertEqual(report["applications"]["Editor.exe"], 3600)
        self.assertEqual(report["domains"]["sub.study.example"], 3600)
        self.assertEqual(
            report["categories"], {"学习办公": 3600, "学习网站": 3600}
        )
        self.assertEqual(len(fake_aw.calls), 1)


if __name__ == "__main__":
    unittest.main()
