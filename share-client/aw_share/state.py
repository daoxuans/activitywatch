"""Durable, local consent windows for sharing ActivityWatch *summaries*.

ActivityWatch itself is independent of this opt-in. Pausing or revoking this
state only stops this client's sharing; it does not stop local AW watchers.
All stored intervals are half-open UTC intervals. An open interval is clipped
to ``now`` when read, so activity before the first opt-in or during a pause
can never be added by resuming later.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit


class StateError(RuntimeError):
    """A local sharing state could not be loaded or safely persisted."""


def _utc(instant: datetime | None) -> datetime:
    if instant is None:
        return datetime.now(timezone.utc)
    if not isinstance(instant, datetime) or instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("授权时间必须带时区")
    return instant.astimezone(timezone.utc)


def _encode(instant: datetime) -> str:
    return instant.isoformat().replace("+00:00", "Z")


def _decode(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("invalid datetime")
    return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _receipt_key(day: str, kind: str) -> str:
    if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
        raise ValueError("invalid report day")
    if kind not in ("current", "daily"):
        raise ValueError("invalid report kind")
    return f"{day}:{kind}"


def canonical_endpoint(endpoint: str) -> str:
    """Accept one HTTPS destination, without credentials or URL parameters."""
    if not isinstance(endpoint, str) or not endpoint or endpoint != endpoint.strip():
        raise ValueError("接收地址必须是完整的 HTTPS URL")
    try:
        parts = urlsplit(endpoint)
        if (
            parts.scheme.lower() != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or any(ord(char) < 33 or ord(char) == 127 for char in endpoint)
        ):
            raise ValueError("invalid endpoint")
        host = parts.hostname.encode("idna").decode("ascii").lower()
        port = parts.port
        if ":" in host:  # IPv6 literals need brackets in an authority.
            host = f"[{host}]"
        authority = f"{host}:{port}" if port not in (None, 443) else host
        return urlunsplit(("https", authority, parts.path or "/", "", ""))
    except (ValueError, UnicodeError) as exc:
        raise ValueError("接收地址必须是完整的 HTTPS URL，且不得含凭证或查询参数") from None


@contextmanager
def _write_lock(path: Path) -> Iterator[None]:
    """Serialize consent changes and acknowledgements across CLI processes."""
    descriptor = -1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield
        finally:
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.name == "nt":
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError:
        raise StateError("无法锁定本机共享状态；已停止上传") from None
    finally:
        if descriptor != -1:
            os.close(descriptor)


class SharingState:
    """Explicit opt-in persisted to a small, local JSON file.

    ``load`` never enables sharing on its own. A missing file is disabled;
    a corrupt file fails closed rather than silently creating a new consent.
    The API's ``at`` and interval boundaries must be timezone-aware.
    """

    def __init__(
        self,
        path: Path,
        device_id: str,
        intervals: list[tuple[datetime, datetime | None]],
        last_changed_at: datetime | None,
        sent_reports: dict[str, str],
        approved_endpoint: str | None,
        consent_generation: int,
    ) -> None:
        self.path = path
        self.device_id = device_id
        self._intervals = intervals
        self.last_changed_at = last_changed_at
        self._sent_reports = sent_reports
        self.approved_endpoint = approved_endpoint
        self.consent_generation = consent_generation

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> SharingState:
        local_path = Path(path)
        try:
            raw = json.loads(local_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls(local_path, str(uuid.uuid4()), [], None, {}, None, 0)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise StateError("无法读取本机共享状态；已停止上传") from None

        try:
            if (
                not isinstance(raw, dict)
                or isinstance(raw.get("schema_version"), bool)
                or raw.get("schema_version") != 1
            ):
                raise ValueError("invalid state schema")
            device_id = str(uuid.UUID(raw["device_id"]))
            if device_id != raw["device_id"]:
                raise ValueError("invalid device ID")
            last_changed_at = (
                _decode(raw["last_changed_at"])
                if raw.get("last_changed_at") is not None
                else None
            )
            rows = raw["intervals"]
            if not isinstance(rows, list):
                raise ValueError("invalid intervals")
            intervals: list[tuple[datetime, datetime | None]] = []
            previous_end: datetime | None = None
            for index, row in enumerate(rows):
                if not isinstance(row, dict) or set(row) != {"start_utc", "end_utc"}:
                    raise ValueError("invalid interval")
                start = _decode(row["start_utc"])
                end = _decode(row["end_utc"]) if row.get("end_utc") is not None else None
                if (end is not None and end <= start) or (
                    index and (previous_end is None or start < previous_end)
                ):
                    raise ValueError("overlapping or inverted intervals")
                if end is None and index != len(rows) - 1:
                    raise ValueError("open interval not last")
                intervals.append((start, end))
                previous_end = end
            if intervals and last_changed_at is None:
                raise ValueError("missing transition time")
            if intervals:
                last_edge = intervals[-1][1] or intervals[-1][0]
                if last_changed_at < last_edge:
                    raise ValueError("transition time before interval")
            approved_endpoint = raw.get("approved_endpoint")
            if approved_endpoint is not None:
                if canonical_endpoint(approved_endpoint) != approved_endpoint:
                    raise ValueError("noncanonical endpoint")
            elif intervals:
                raise ValueError("intervals without approved endpoint")
            generation = raw["consent_generation"]
            if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
                raise ValueError("invalid consent generation")
            receipts = raw.get("sent_reports", {})
            if not isinstance(receipts, dict):
                raise ValueError("invalid receipts")
            for key, report_id in receipts.items():
                if not isinstance(key, str) or ":" not in key:
                    raise ValueError("invalid receipt key")
                day, kind = key.split(":", 1)
                _receipt_key(day, kind)
                if not isinstance(report_id, str) or len(report_id) != 64 or any(
                    char not in "0123456789abcdef" for char in report_id
                ):
                    raise ValueError("invalid receipt id")
            return cls(
                local_path, device_id, intervals, last_changed_at,
                receipts, approved_endpoint, generation,
            )
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise StateError("本机共享状态无效；已停止上传") from None

    @property
    def enabled(self) -> bool:
        return bool(self._intervals and self._intervals[-1][1] is None)

    def refresh(self) -> SharingState:
        """Reload disk state; callers must do this before sending a summary."""
        current = self.load(self.path)
        self.device_id = current.device_id
        self._intervals = current._intervals
        self.last_changed_at = current.last_changed_at
        self._sent_reports = current._sent_reports
        self.approved_endpoint = current.approved_endpoint
        self.consent_generation = current.consent_generation
        return self

    def _save(
        self,
        intervals: list[tuple[datetime, datetime | None]],
        changed: datetime | None,
        receipts: dict[str, str],
        endpoint: str | None,
        generation: int,
    ) -> None:
        payload = {
            "schema_version": 1,
            "device_id": self.device_id,
            "last_changed_at": _encode(changed) if changed is not None else None,
            "approved_endpoint": endpoint,
            "consent_generation": generation,
            "intervals": [
                {"start_utc": _encode(start), "end_utc": _encode(end) if end else None}
                for start, end in intervals
            ],
            "sent_reports": receipts,
        }
        temporary: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=".sharing-", dir=self.path.parent)
            try:
                os.chmod(temporary, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    descriptor = -1
                    json.dump(payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                if descriptor != -1:
                    os.close(descriptor)
            os.replace(temporary, self.path)
            temporary = None
        except OSError:
            raise StateError("无法保存本机共享状态；已停止上传") from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

        self._intervals = intervals
        self.last_changed_at = changed
        self._sent_reports = receipts
        self.approved_endpoint = endpoint
        self.consent_generation = generation

    def _transition_at(self, at: datetime | None) -> datetime:
        instant = _utc(at)
        if self.last_changed_at is not None and instant < self.last_changed_at:
            raise ValueError("系统时间早于上次授权变更；请校准时钟")
        return instant

    def enable(self, at: datetime | None = None, *, endpoint: str) -> bool:
        """Open a new interval, only when a local user explicitly opts in."""
        destination = canonical_endpoint(endpoint)
        with _write_lock(self.path):
            self.refresh()
            if self.enabled:
                if self.approved_endpoint != destination:
                    raise ValueError("更换接收地址须先暂停，再重新确认启用")
                return False
            instant = self._transition_at(at)
            # Consent for the old recipient must never be reused for a new
            # recipient, including earlier windows on the same calendar day.
            changed_recipient = self.approved_endpoint not in (None, destination)
            previous_intervals = [] if changed_recipient else self._intervals
            previous_receipts = {} if changed_recipient else self._sent_reports.copy()
            self._save(
                [*previous_intervals, (instant, None)], instant, previous_receipts,
                destination, self.consent_generation + 1,
            )
            return True

    def pause(self, at: datetime | None = None) -> bool:
        """Stop sharing now; a later ``enable`` never fills this gap."""
        with _write_lock(self.path):
            self.refresh()
            if not self.enabled:
                return False
            instant = self._transition_at(at)
            start, _ = self._intervals[-1]
            if instant == start:
                intervals = self._intervals[:-1]
            else:
                intervals = [*self._intervals[:-1], (start, instant)]
            self._save(
                intervals, instant, self._sent_reports.copy(),
                self.approved_endpoint, self.consent_generation + 1,
            )
            return True

    def revoke(self, at: datetime | None = None) -> None:
        """Disable sharing and forget past local authorization windows.

        This cannot retract anything that was already delivered to a remote
        service. Cloud-side deletion must be implemented by the service.
        """
        with _write_lock(self.path):
            self.refresh()
            instant = self._transition_at(at)
            self._save([], instant, {}, None, self.consent_generation + 1)

    def allowed_intervals(
        self, start: datetime, end: datetime, now: datetime | None = None
    ) -> list[tuple[datetime, datetime]]:
        """Return UTC windows intersecting ``[start, end)`` and before now."""
        begin, finish, cutoff = _utc(start), _utc(end), _utc(now)
        if finish < begin:
            raise ValueError("interval end precedes start")
        finish = min(finish, cutoff)
        if finish <= begin:
            return []
        result = []
        for first, last in self._intervals:
            left, right = max(first, begin), min(last or finish, finish)
            if left < right:
                result.append((left, right))
        return result

    def was_sent(self, report_id: str) -> bool:
        """Only successful server acknowledgements are recorded here."""
        return report_id in self._sent_reports.values()

    def has_sent_day(self, day: date | str, kind: str = "daily") -> bool:
        """Whether this day/kind has a successful receipt (refresh first)."""
        value = day.isoformat() if isinstance(day, date) else day
        return _receipt_key(value, kind) in self._sent_reports

    def mark_sent(
        self, day: str, kind: str, report_id: str, *, expected_generation: int | None = None
    ) -> bool:
        """Record a 2xx only if the same consent is still active.

        A remote 2xx can race with a local pause. The upload did happen, but
        the old report must not restore an authorization or receipt after a
        pause, revocation, or change of recipient.
        """
        key = _receipt_key(day, kind)
        if not isinstance(report_id, str) or len(report_id) != 64 or any(
            char not in "0123456789abcdef" for char in report_id
        ):
            raise ValueError("invalid report ID")
        with _write_lock(self.path):
            self.refresh()  # Never restore stale consent after a concurrent pause.
            if not self.enabled or (
                expected_generation is not None
                and self.consent_generation != expected_generation
            ):
                return False
            receipts = {**self._sent_reports, key: report_id}
            self._save(
                self._intervals.copy(), self.last_changed_at, receipts,
                self.approved_endpoint, self.consent_generation,
            )
            return True

