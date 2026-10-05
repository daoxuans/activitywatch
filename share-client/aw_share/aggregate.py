"""Turn local ActivityWatch events into non-event-level daily summaries.

Window and browser events can contain sensitive titles and full URLs. They are
read in memory only. The returned value contains application basenames, domain
hosts, category names, durations, and coverage metadata -- never raw events.
"""

from __future__ import annotations

import ipaddress
import math
import ntpath
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any
from urllib.parse import urlsplit


class DataUnavailable(RuntimeError):
    """Required local watcher data is missing or cannot be interpreted."""


@dataclass(frozen=True)
class _Span:
    start: datetime
    end: datetime
    data: Mapping[str, Any]
    bucket_id: str = ""
    order: int = 0


_DEFAULT_BROWSERS: dict[str, tuple[str, ...]] = {
    "chrome": ("chrome.exe", "google chrome"),
    "edge": ("msedge.exe", "microsoft edge"),
    "firefox": ("firefox.exe", "mozilla firefox"),
    "brave": ("brave.exe", "brave browser"),
    "opera": ("opera.exe", "opera browser"),
    "chromium": ("chromium.exe", "chromium"),
    "vivaldi": ("vivaldi.exe", "vivaldi"),
    "arc": ("arc.exe", "arc"),
    "zen": ("zen.exe", "zen browser"),
    "floorp": ("floorp.exe", "floorp"),
    "helium": ("helium.exe", "helium"),
    "yandex": ("yandexbrowser.exe", "yandex browser"),
}


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("timestamp is not an ISO datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp has no timezone")
    return parsed


def _event_spans(
    events: Sequence[Mapping[str, Any]], start: datetime, end: datetime
) -> list[_Span]:
    spans: list[_Span] = []
    for order, event in enumerate(events):
        try:
            timestamp = _parse_timestamp(event["timestamp"])
            duration = float(event["duration"])
            if not math.isfinite(duration) or duration <= 0:
                continue
            data = event.get("data", {})
            if not isinstance(data, Mapping):
                continue
            stop = timestamp + timedelta(seconds=duration)
            clipped_start, clipped_end = max(timestamp, start), min(stop, end)
            if clipped_start < clipped_end:
                spans.append(
                    _Span(
                        clipped_start,
                        clipped_end,
                        data,
                        str(event.get("bucket_id", "")),
                        order,
                    )
                )
        except (KeyError, TypeError, ValueError, OverflowError):
            # Do not include raw event content in exceptions or logs.
            continue
    return spans


def _allowed_spans(
    intervals: Sequence[tuple[datetime, datetime]], start: datetime, end: datetime
) -> list[_Span]:
    spans: list[_Span] = []
    for order, (first, last) in enumerate(intervals):
        if first.tzinfo is None or last.tzinfo is None:
            raise ValueError("sharing intervals must be timezone-aware")
        clipped_start, clipped_end = max(first, start), min(last, end)
        if clipped_start < clipped_end:
            spans.append(_Span(clipped_start, clipped_end, {}, order=order))
    return spans


def _application_name(value: Any) -> str:
    raw = value if isinstance(value, str) else ""
    name = ntpath.basename(raw.replace("/", "\\")).strip()
    name = "".join(char for char in name if char.isprintable())[:120]
    return name or "未知软件"


def _domain_from_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in ("http", "https"):
            return None
        host = (parsed.hostname or "").rstrip(".").lower()
        if host.startswith("www."):
            host = host[4:]
        if not host or host == "localhost" or host.endswith(".local"):
            return None
        try:
            ipaddress.ip_address(host)
            return None
        except ValueError:
            ascii_host = host.encode("idna").decode("ascii")
            labels = ascii_host.split(".")
            if len(ascii_host) > 253 or any(
                len(label) > 63
                or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
                for label in labels
            ):
                return None
            return ascii_host
    except (UnicodeError, ValueError):
        return None


def _is_incognito(value: Any) -> bool:
    # aw-watcher-web v0.5.0 briefly emitted the strings "true"/"false".
    # An unknown nonempty value must never be treated as definitely public.
    if value is None or value is False:
        return False
    if isinstance(value, str) and value.casefold() == "false":
        return False
    return True


def _browser_identity(
    app_name: str,
    browser_apps: Mapping[str, Sequence[str]] | set[str] | None,
) -> str | None:
    name = app_name.casefold()
    if browser_apps is None:
        mapping: Mapping[str, Sequence[str]] = _DEFAULT_BROWSERS
    elif isinstance(browser_apps, Mapping):
        mapping = {**_DEFAULT_BROWSERS, **browser_apps}
    else:
        mapping = {
            item.casefold().removesuffix(".exe"): (item,)
            for item in browser_apps
        }
    for browser, apps in mapping.items():
        if name in {item.casefold() for item in apps}:
            return browser.casefold()
    return None


def _category_for(value: str, rules: Mapping[str, str] | None, *, domain: bool) -> str:
    if not rules:
        return "未分类"
    target = value.casefold()
    for key, category in sorted(rules.items(), key=lambda pair: len(pair[0]), reverse=True):
        candidate = key.casefold().lstrip(".")
        matches = target == candidate or (domain and target.endswith("." + candidate))
        if matches:
            return category
    return "未分类"


def _web_matches_browser(bucket_id: str, browser: str) -> bool:
    # ActivityWatch browser buckets commonly look like
    # aw-watcher-web-chrome_<hostname>. Never search the full bucket ID for a
    # browser substring: the *hostname* may itself contain "Edge" or "Chrome".
    prefix = "aw-watcher-web-"
    lowered = bucket_id.casefold()
    if not lowered.startswith(prefix):
        return False
    watcher = lowered[len(prefix):].split("_", 1)[0]
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", watcher):
        # An unknown source cannot safely be assigned to whichever browser
        # happens to be in the foreground. Report its time as uncovered.
        return False
    return watcher == browser or (browser == "chromium" and watcher == "chrome")


def _seconds(values: Mapping[str, int]) -> dict[str, float]:
    return {key: round(milliseconds / 1000, 3) for key, milliseconds in sorted(values.items())}


def summarize_day(
    day: date,
    timezone: tzinfo,
    now: datetime,
    window_events: Sequence[Mapping[str, Any]],
    afk_events: Sequence[Mapping[str, Any]],
    web_events: Sequence[Mapping[str, Any]],
    allowed_intervals: Sequence[tuple[datetime, datetime]],
    app_categories: Mapping[str, str] | None = None,
    domain_categories: Mapping[str, str] | None = None,
    browser_apps: Mapping[str, Sequence[str]] | set[str] | None = None,
) -> dict[str, Any]:
    """Summarize only intervals when sharing was explicitly enabled.

    Application totals include browser foreground time. Domain totals are a
    *second view* of a subset of browser time, never added to application or
    category totals. A missing required watcher is not interpreted as zero use.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    start = datetime.combine(day, time.min, tzinfo=timezone)
    end = min(
        datetime.combine(day + timedelta(days=1), time.min, tzinfo=timezone),
        now.astimezone(timezone),
    )
    if end <= start:
        raise ValueError("day has not started")
    if not window_events or not afk_events:
        raise DataUnavailable("ActivityWatch window or AFK events are unavailable")

    windows = _event_spans(window_events, start, end)
    afk = _event_spans(afk_events, start, end)
    web = _event_spans(web_events, start, end)
    allowed = _allowed_spans(allowed_intervals, start, end)
    if not windows or not afk:
        raise DataUnavailable("ActivityWatch window or AFK coverage is unavailable")

    groups = {"window": windows, "afk": afk, "web": web, "allowed": allowed}
    markers: dict[datetime, list[tuple[int, str, int]]] = defaultdict(list)
    for kind, spans in groups.items():
        for index, span in enumerate(spans):
            markers[span.start].append((1, kind, index))
            markers[span.end].append((0, kind, index))
    points = sorted({start, end, *markers})
    active: dict[str, dict[int, _Span]] = {kind: {} for kind in groups}

    total_ms = 0
    authorized_ms = 0
    observed_ms = 0
    uncovered_ms = 0
    last_observed_at: datetime | None = None
    applications: dict[str, int] = defaultdict(int)
    domains: dict[str, int] = defaultdict(int)
    categories: dict[str, int] = defaultdict(int)
    category_applications: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    category_domains: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for index, point in enumerate(points[:-1]):
        for action, kind, item in sorted(markers.get(point, [])):
            if action == 0:
                active[kind].pop(item, None)
            else:
                active[kind][item] = groups[kind][item]
        next_point = points[index + 1]
        if not active["allowed"]:
            continue
        milliseconds = round((next_point - point).total_seconds() * 1000)
        if milliseconds <= 0:
            continue
        authorized_ms += milliseconds
        if not active["window"] or not active["afk"]:
            continue
        current_afk = max(active["afk"].values(), key=lambda span: (span.start, span.order))
        if current_afk.data.get("status") not in ("afk", "not-afk"):
            continue
        observed_ms += milliseconds
        last_observed_at = next_point
        if current_afk.data.get("status") != "not-afk":
            continue
        current_window = max(
            active["window"].values(), key=lambda span: (span.start, span.order)
        )
        application = _application_name(current_window.data.get("app"))
        browser = _browser_identity(application, browser_apps)
        domain_name: str | None = None
        if browser:
            candidates = [
                (span, _domain_from_url(span.data.get("url")))
                for span in active["web"].values()
                if not _is_incognito(span.data.get("incognito"))
                and _web_matches_browser(span.bucket_id, browser)
            ]
            valid = [(span, name) for span, name in candidates if name]
            if valid:
                _, domain_name = max(valid, key=lambda pair: (pair[0].start, pair[0].order))
        if domain_name:
            category = _category_for(domain_name, domain_categories, domain=True)
            domains[domain_name] += milliseconds
            category_domains[category][domain_name] += milliseconds
        elif browser:
            category = "网页未覆盖"
            uncovered_ms += milliseconds
        else:
            category = _category_for(application, app_categories, domain=False)
        total_ms += milliseconds
        applications[application] += milliseconds
        categories[category] += milliseconds
        category_applications[category][application] += milliseconds

    if observed_ms == 0:
        raise DataUnavailable("ActivityWatch window and AFK coverage do not overlap")
    unobserved_ms = max(authorized_ms - observed_ms, 0)
    return {
        "day": day.isoformat(),
        "data_cutoff": end.isoformat(),
        "authorized": bool(allowed),
        "total_seconds": round(total_ms / 1000, 3),
        "applications": _seconds(applications),
        "domains": _seconds(domains),
        "categories": _seconds(categories),
        "category_applications": {
            category: _seconds(values) for category, values in sorted(category_applications.items())
        },
        "category_domains": {
            category: _seconds(values) for category, values in sorted(category_domains.items())
        },
        "web_uncovered_seconds": round(uncovered_ms / 1000, 3),
        "website_coverage": "partial" if uncovered_ms or unobserved_ms else "complete",
        "activity_coverage": "partial" if unobserved_ms else "complete",
        "unobserved_seconds": round(unobserved_ms / 1000, 3),
        "last_observed_at": last_observed_at.astimezone(timezone).isoformat(),
    }
