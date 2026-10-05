"""Send allowlisted ActivityWatch summaries to one consented HTTPS endpoint.

The transport never accepts ActivityWatch events, titles, full URLs or file
paths. Each distinct canonical snapshot gets a deterministic report ID; an
identical retry sends identical bytes and uses the same idempotency key.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import date, datetime, time as daytime, timedelta
from typing import Any

from .state import SharingState, canonical_endpoint


class ConfigurationError(ValueError):
    """The HTTPS endpoint, token, or transport settings are unusable."""


class UploadError(RuntimeError):
    """A snapshot was not acknowledged as delivered."""


class AuthenticationError(UploadError):
    """The receiver rejected its bearer token."""


class ConsentChangedError(UploadError):
    """Consent was paused, revoked, or changed during collection."""


_SUMMARY_FIELDS = frozenset(
    {
        "day",
        "data_cutoff",
        "authorized",
        "total_seconds",
        "applications",
        "domains",
        "categories",
        "category_applications",
        "category_domains",
        "web_uncovered_seconds",
        "website_coverage",
        "activity_coverage",
        "unobserved_seconds",
        "last_observed_at",
    }
)
_SAFE_DEVICE = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_FORBIDDEN_CATEGORY_CHARS = frozenset("/\\:?#@%")
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


def _label(value: Any, kind: str) -> str:
    max_length = 253 if kind == "domain" else 120
    forbidden = frozenset("/\\:") if kind == "app" else _FORBIDDEN_CATEGORY_CHARS
    if (
        not isinstance(value, str)
        or not value
        or len(value) > max_length
        or value != value.strip()
        or any(not char.isprintable() or char in forbidden for char in value)
    ):
        raise ValueError("汇总标签无效；拒绝上传")
    if kind == "domain":
        try:
            # Every component must be a DNS hostname label. IDNs are allowed
            # through their ASCII form; URL paths, userinfo and IP literals
            # cannot masquerade as website domains.
            ascii_host = value.encode("idna").decode("ascii")
        except UnicodeError:
            raise ValueError("域名标签无效；拒绝上传") from None
        if len(ascii_host) > 253 or any(
            not _HOST_LABEL.fullmatch(part) for part in ascii_host.split(".")
        ):
            raise ValueError("域名标签无效；拒绝上传")
        try:
            ipaddress.ip_address(ascii_host)
        except ValueError:
            return value  # Not an IP literal: a valid hostname.
        raise ValueError("域名标签无效；拒绝上传")
    return value


def _seconds(value: Any) -> int | float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError("汇总时长无效；拒绝上传")
    return value


def _totals(value: Any, kind: str) -> dict[str, int | float]:
    if not isinstance(value, Mapping) or len(value) > 5000:
        raise ValueError("汇总结构无效；拒绝上传")
    return {_label(name, kind): _seconds(seconds) for name, seconds in value.items()}


def _nested_totals(value: Any, member_kind: str) -> dict[str, dict[str, int | float]]:
    if not isinstance(value, Mapping) or len(value) > 5000:
        raise ValueError("汇总结构无效；拒绝上传")
    return {_label(category, "category"): _totals(members, member_kind) for category, members in value.items()}


def _aware_timestamp(value: Any, error_message: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(error_message)
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(error_message) from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(error_message)
    return result


def _safe_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Fail closed on any field or shape outside the aggregate schema."""
    if not isinstance(summary, Mapping) or set(summary) != _SUMMARY_FIELDS:
        raise ValueError("汇总字段不符合允许上传的结构")
    day = summary["day"]
    if not isinstance(day, str):
        raise ValueError("汇总日期无效")
    try:
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError("invalid day")
    except ValueError:
        raise ValueError("汇总日期无效") from None
    cutoff = summary["data_cutoff"]
    parsed = _aware_timestamp(cutoff, "汇总截止时间无效或无时区")
    if summary["authorized"] is not True:
        raise ValueError("未获分享授权的汇总不得上传")
    web_coverage = summary["website_coverage"]
    if web_coverage not in ("complete", "partial"):
        raise ValueError("网页覆盖状态无效")
    activity_coverage = summary["activity_coverage"]
    if activity_coverage not in ("complete", "partial"):
        raise ValueError("活动采集覆盖状态无效")
    unobserved = _seconds(summary["unobserved_seconds"])
    if (activity_coverage == "complete") != (unobserved == 0):
        raise ValueError("活动采集覆盖状态与未观测时长不匹配")
    observed_at = summary["last_observed_at"]
    if observed_at is not None:
        observed_at = _aware_timestamp(observed_at, "最后观测时间无效或无时区")
        if observed_at > parsed:
            raise ValueError("最后观测时间晚于汇总请求截止")
    safe = {
        "day": day,
        "data_cutoff": parsed.isoformat(),
        "authorized": True,
        "total_seconds": _seconds(summary["total_seconds"]),
        "applications": _totals(summary["applications"], "app"),
        "domains": _totals(summary["domains"], "domain"),
        "categories": _totals(summary["categories"], "category"),
        "category_applications": _nested_totals(summary["category_applications"], "app"),
        "category_domains": _nested_totals(summary["category_domains"], "domain"),
        "web_uncovered_seconds": _seconds(summary["web_uncovered_seconds"]),
        "website_coverage": web_coverage,
        "activity_coverage": activity_coverage,
        "unobserved_seconds": unobserved,
        "last_observed_at": observed_at.isoformat() if observed_at is not None else None,
    }
    if any(category not in safe["categories"] for category in safe["category_applications"]):
        raise ValueError("软件分类汇总无效")
    if any(category not in safe["categories"] for category in safe["category_domains"]):
        raise ValueError("域名分类汇总无效")
    if any(
        name not in safe["applications"]
        for members in safe["category_applications"].values()
        for name in members
    ):
        raise ValueError("软件明细与汇总不匹配")
    if any(
        name not in safe["domains"]
        for members in safe["category_domains"].values()
        for name in members
    ):
        raise ValueError("域名明细与汇总不匹配")
    return safe


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def prepare_report(
    summary: Mapping[str, Any], *, device_id: str, kind: str = "current"
) -> tuple[str, bytes]:
    """Create an idempotent, strictly aggregate-only request body."""
    if not isinstance(device_id, str) or not _SAFE_DEVICE.fullmatch(device_id):
        raise ValueError("设备标识无效")
    if kind not in ("current", "daily"):
        raise ValueError("上报类型无效")
    fields = {
        "schema_version": 1,
        "device_id": device_id,
        "kind": kind,
        "summary": _safe_summary(summary),
    }
    report_id = hashlib.sha256(_json_bytes(fields)).hexdigest()
    body = _json_bytes({**fields, "report_id": report_id})
    if len(body) > 512 * 1024:
        raise ValueError("汇总过大；拒绝上传")
    return report_id, body


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        # Even an HTTPS URL might redirect to HTTP or to a different receiver.
        return None


def _https_transport(request: urllib.request.Request, timeout: float) -> int:
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(request, timeout=timeout) as response:
        return int(response.status)


class SummaryUploader:
    """POST current/daily snapshots with bounded retries and consent checks.

    A test may inject ``transport(request, timeout) -> HTTP status int``.
    Production uses verified TLS and rejects redirects. Tokens are accepted
    explicitly or read from ``AW_SHARE_API_TOKEN``; neither is persisted.
    """

    def __init__(
        self,
        endpoint: str,
        token: str | None = None,
        *,
        transport: Callable[[urllib.request.Request, float], int] | None = None,
        timeout: float = 10.0,
        max_retries: int = 2,
        retry_delay: float = 0.2,
    ) -> None:
        try:
            self.endpoint = canonical_endpoint(endpoint)
        except ValueError:
            raise ConfigurationError("上报接收地址必须是 HTTPS") from None
        secret = os.environ.get("AW_SHARE_API_TOKEN") if token is None else token
        if (
            not isinstance(secret, str)
            or not secret
            or any(ord(char) < 33 or ord(char) == 127 for char in secret)
        ):
            raise ConfigurationError("缺少有效令牌；请设置 AW_SHARE_API_TOKEN")
        self._token = secret
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ConfigurationError("上报超时配置无效")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or not 0 <= max_retries <= 10:
            raise ConfigurationError("重试次数配置无效")
        if not isinstance(retry_delay, (int, float)) or not math.isfinite(retry_delay) or not 0 <= retry_delay <= 60:
            raise ConfigurationError("重试间隔配置无效")
        self.timeout = float(timeout)
        self.max_retries = max_retries
        self.retry_delay = float(retry_delay)
        self._transport = transport or _https_transport

    def _check_consent(
        self, state: SharingState, generation: int, device_id: str, summary: Mapping[str, Any]
    ) -> None:
        state.refresh()
        if (
            not state.enabled
            or state.consent_generation != generation
            or state.approved_endpoint != self.endpoint
            or state.device_id != device_id
        ):
            raise ConsentChangedError("本机分享授权或接收地址已变更；旧汇总未发送")

        # Prevent a handcrafted, previously unauthorized day's report from
        # being sent merely because sharing is currently enabled.
        cutoff = datetime.fromisoformat(summary["data_cutoff"])
        local_day = date.fromisoformat(summary["day"])
        begin = datetime.combine(local_day, daytime.min, tzinfo=cutoff.tzinfo)
        finish = datetime.combine(local_day + timedelta(days=1), daytime.min, tzinfo=cutoff.tzinfo)
        allowed = state.allowed_intervals(begin, finish, now=cutoff)
        if not allowed:
            raise ConsentChangedError("汇总日期不在本机授权分享时段内")
        authorized_seconds = sum((end - start).total_seconds() for start, end in allowed)
        if summary["total_seconds"] + summary["unobserved_seconds"] > authorized_seconds + 0.002:
            raise ConsentChangedError("汇总时长超出本机获准分享时段")

    def upload(
        self,
        summary: Mapping[str, Any],
        *,
        device_id: str,
        state: SharingState,
        kind: str = "current",
        expected_generation: int | None = None,
    ) -> str:
        """Return report_id only after 2xx or a previously recorded 2xx.

        ``expected_generation`` should be captured before reading AW events.
        Pausing or reauthorizing while collection is in progress then rejects
        the stale snapshot. Each network retry uses the same body and ID.
        """
        generation = state.consent_generation if expected_generation is None else expected_generation
        report_id, body = prepare_report(summary, device_id=device_id, kind=kind)
        safe_summary = json.loads(body)["summary"]
        self._check_consent(state, generation, device_id, safe_summary)
        if state.was_sent(report_id):
            return report_id

        for attempt in range(self.max_retries + 1):
            self._check_consent(state, generation, device_id, safe_summary)
            request = urllib.request.Request(
                self.endpoint,
                data=body,
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Authorization": f"Bearer {self._token}",
                    "Idempotency-Key": report_id,
                },
                method="POST",
            )
            try:
                result = self._transport(request, self.timeout)
                status = int(result if isinstance(result, int) else result.status)
            except urllib.error.HTTPError as exc:
                status = exc.code
            except Exception:
                # Never expose exceptions that may contain URL, token or body.
                status = None

            if status is not None and 200 <= status < 300:
                state.mark_sent(
                    safe_summary["day"], kind, report_id,
                    expected_generation=generation,
                )
                return report_id
            if status in (401, 403):
                raise AuthenticationError("接收端认证失败（HTTP 401/403）；未记录成功")
            retryable = status is None or status in (408, 425, 429) or status >= 500
            if not retryable:
                raise UploadError("接收端拒绝汇总；未记录成功")
            if attempt == self.max_retries:
                raise UploadError("网络或接收端暂不可用；未记录成功")
            time.sleep(self.retry_delay * (2**attempt))

        raise AssertionError("unreachable")

