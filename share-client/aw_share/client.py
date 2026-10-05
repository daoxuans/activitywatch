"""Coordinate one consent-bounded ActivityWatch summary and optional upload.

Raw events remain in this process.  The remote uploader receives only the
aggregate returned by :func:`summarize_day`, never watcher event dictionaries.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any

from .aggregate import summarize_day


def run_once(
    day: date,
    now: datetime,
    timezone: tzinfo,
    aw_client: Any,
    state: Any,
    uploader: Any = None,
    app_categories: Mapping[str, str] | None = None,
    domain_categories: Mapping[str, str] | None = None,
    browser_apps: Mapping[str, Sequence[str]] | None = None,
    *,
    upload: bool = True,
) -> dict[str, Any] | None:
    """Return a local summary, and upload it only while sharing is enabled.

    ``None`` means there is no authorized time to share, or sharing is paused.
    Missing watcher data raises an error instead of reporting false zero use.
    Requesting one preceding day works around ActivityWatch query boundaries
    where an event starts before midnight and ends inside the requested day;
    the aggregator still strictly clips it to the chosen day and consent spans.
    """
    if not isinstance(day, date) or isinstance(day, datetime):
        raise ValueError("day must be a date")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if timezone is None:
        raise ValueError("timezone is required")
    local_now = now.astimezone(timezone)
    start = datetime.combine(day, time.min, tzinfo=timezone)
    finish = datetime.combine(day + timedelta(days=1), time.min, tzinfo=timezone)
    end = min(finish, local_now)
    if end <= start:
        raise ValueError("day has not started")
    state.refresh()
    if not state.enabled:
        return None

    generation = state.consent_generation
    authorized = state.allowed_intervals(start, end, now=local_now)
    if not authorized:
        return None

    events = aw_client.fetch_events(start - timedelta(days=1), end)
    state.refresh()
    if not state.enabled or generation != state.consent_generation:
        # A pause/revocation during the local read invalidates the snapshot.
        return None
    summary = summarize_day(
        day=day,
        timezone=timezone,
        now=local_now,
        window_events=events["window"],
        afk_events=events["afk"],
        web_events=events["web"],
        allowed_intervals=authorized,
        app_categories=app_categories,
        domain_categories=domain_categories,
        browser_apps=browser_apps,
    )
    state.refresh()
    if not state.enabled or generation != state.consent_generation:
        return None
    if upload:
        if uploader is None:
            raise ValueError("an uploader is required when upload=True")
        kind = "daily" if finish <= local_now else "current"
        uploader.upload(
            summary,
            device_id=state.device_id,
            state=state,
            kind=kind,
            expected_generation=generation,
        )
    return summary


def preview_local_only(
    day: date,
    now: datetime,
    timezone: tzinfo,
    aw_client: Any,
    app_categories: Mapping[str, str] | None = None,
    domain_categories: Mapping[str, str] | None = None,
    browser_apps: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Display a one-off summary without remote upload or stored sharing state.

    The caller must obtain a fresh, visible confirmation from the computer's
    user. This function has no uploader and does not enable future sharing.
    """
    if not isinstance(day, date) or isinstance(day, datetime):
        raise ValueError("day must be a date")
    if now.tzinfo is None or now.utcoffset() is None or timezone is None:
        raise ValueError("now and timezone must be valid")
    local_now = now.astimezone(timezone)
    start = datetime.combine(day, time.min, tzinfo=timezone)
    end = min(datetime.combine(day + timedelta(days=1), time.min, tzinfo=timezone), local_now)
    if end <= start:
        raise ValueError("day has not started")
    events = aw_client.fetch_events(start - timedelta(days=1), end)
    summary = summarize_day(
        day=day,
        timezone=timezone,
        now=local_now,
        window_events=events["window"],
        afk_events=events["afk"],
        web_events=events["web"],
        allowed_intervals=[(start, end)],
        app_categories=app_categories,
        domain_categories=domain_categories,
        browser_apps=browser_apps,
    )
    # This one-off on-screen view is never a remotely authorized report.
    return {**summary, "authorized": False, "local_only": True}
