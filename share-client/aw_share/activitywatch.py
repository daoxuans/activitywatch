"""Read ActivityWatch's local API without changing or exposing its raw data.

Only a numeric loopback address is contacted. ``localhost`` is normalized to
127.0.0.1, proxies and HTTP redirects are disabled, and no response body is
included in errors. Raw events should be passed directly to the local summary
builder; they must never be sent to the remote reporting endpoint.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import socket
from collections.abc import Mapping
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class ActivityWatchError(RuntimeError):
    """The local server or its required watcher data is unavailable."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        # A loopback service must not be able to redirect sensitive requests
        # to an external host.
        return None


def _local_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        if host == "localhost":
            host = "127.0.0.1"
        address = ipaddress.ip_address(host or "")
    except (TypeError, ValueError) as exc:
        raise ActivityWatchError("ActivityWatch URL must use a loopback address") from None

    if (
        parsed.scheme not in ("http", "https")
        or not address.is_loopback
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") != "/api/0"
    ):
        raise ActivityWatchError("ActivityWatch URL must point to a local /api/0 endpoint")

    netloc = f"[{host}]" if address.version == 6 else host
    if port is not None:
        netloc += f":{port}"
    return urlunsplit((parsed.scheme, netloc, "/api/0", "", ""))


def _iso_time(value: datetime | str) -> tuple[str, datetime]:
    try:
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(value.replace("Z", "+00:00"))
        )
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone required")
        return parsed.isoformat(), parsed
    except (AttributeError, TypeError, ValueError):
        raise ActivityWatchError("ActivityWatch query times must include a timezone") from None


def _server_identity(info: Mapping) -> tuple[str, str | None]:
    """Get stable, non-sensitive fields needed to reject imported buckets."""
    hostname = info.get("hostname")
    device_id = info.get("device_id")
    if not isinstance(hostname, str) or not hostname:
        raise ActivityWatchError("ActivityWatch local server identity is unavailable")
    if device_id is not None and (not isinstance(device_id, str) or not device_id):
        raise ActivityWatchError("ActivityWatch local server identity is invalid")
    return hostname.casefold(), device_id


def _windows_physical_dns_hostname() -> str | None:
    """Use the same Windows hostname source as ActivityWatch's Rust server."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        get_name = ctypes.WinDLL("kernel32", use_last_error=True).GetComputerNameExW
        get_name.argtypes = (wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
        get_name.restype = wintypes.BOOL
        # ComputerNamePhysicalDnsHostname (5) is what gethostname 0.4 uses.
        size = wintypes.DWORD(0)
        get_name(5, None, ctypes.byref(size))
        if not size.value:
            return None
        buffer = ctypes.create_unicode_buffer(size.value)
        if not get_name(5, buffer, ctypes.byref(size)):
            return None
        return buffer.value
    except (AttributeError, OSError, ValueError):
        return None


def _short_hostname(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    short = value.rstrip(".").partition(".")[0].casefold()
    return short if short and short not in ("unknown", "!local") else None


def _require_local_server(hostname: str) -> None:
    """Reject a loopback tunnel to a server on a different computer.

    Hostnames are an extra guard, not authentication: short names can collide.
    Windows ActivityWatch has used both Winsock and physical DNS hostnames.
    """
    try:
        winsock_name = socket.gethostname()
    except OSError:
        winsock_name = None
    physical_name = _windows_physical_dns_hostname()
    local_names = {
        short
        for name in (winsock_name, physical_name)
        if (short := _short_hostname(name)) is not None
    }
    if _short_hostname(hostname) not in local_names:
        raise ActivityWatchError("ActivityWatch server is not on this computer")


def _is_local_bucket(metadata: Mapping, hostname: str, device_id: str | None) -> bool:
    """Prefer device IDs when both sides have them; fall back to hostname.

    Older window/AFK watchers may not provide ``data.device_id``. Older web
    buckets may have an ``unknown`` hostname; those cannot safely be assigned
    to this device unless a matching device ID is present.
    """
    data = metadata.get("data", {})
    if not isinstance(data, Mapping):
        return False
    bucket_device_id = data.get("device_id")
    if bucket_device_id is not None:
        if not isinstance(bucket_device_id, str) or not bucket_device_id:
            return False
        if device_id is not None:
            return bucket_device_id == device_id
    bucket_hostname = metadata.get("hostname")
    return (
        isinstance(bucket_hostname, str)
        and bucket_hostname.casefold() == hostname
        and hostname not in ("unknown", "!local")
    )


class ActivityWatchClient:
    """Small read-only client for one local ActivityWatch instance.

    ``api_key`` is only for a local server that has opted into Bearer-token
    authentication. The key is never included in error messages.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:5600/api/0",
        *,
        timeout: float = 5.0,
        api_key: str | None = None,
    ) -> None:
        self.base_url = _local_base_url(base_url)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ActivityWatchError("ActivityWatch timeout must be positive")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ActivityWatchError("ActivityWatch timeout must be positive")
        if api_key is not None and (
            not isinstance(api_key, str)
            or not api_key
            or any(ord(char) < 32 or ord(char) == 127 for char in api_key)
        ):
            raise ActivityWatchError("Invalid local ActivityWatch API key")
        self.timeout = float(timeout)
        self._api_key = api_key
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def _get(self, path: str, params: Mapping[str, str] | None = None):
        url = f"{self.base_url}/{path}"
        if params:
            url += "?" + urlencode(params)
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = Request(url, headers=headers, method="GET")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                payload = response.read()
        except HTTPError as exc:
            if exc.code == 401:
                message = "ActivityWatch local API denied access (HTTP 401)"
            elif 300 <= exc.code < 400:
                message = "ActivityWatch local API attempted a redirect"
            else:
                message = f"ActivityWatch local API returned HTTP {exc.code}"
            # HTTPError owns the response stream, even when we intentionally
            # discard its body to avoid exposing event details in diagnostics.
            exc.close()
            raise ActivityWatchError(message) from None
        except (URLError, OSError, TimeoutError) as exc:
            reason = exc.reason if isinstance(exc, URLError) else exc
            if isinstance(reason, (socket.timeout, TimeoutError)):
                message = "ActivityWatch local API timed out"
            else:
                message = "Cannot reach the local ActivityWatch server"
            raise ActivityWatchError(message) from None
        try:
            return json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ActivityWatchError("ActivityWatch local API returned invalid JSON") from None

    def get_info(self) -> dict:
        """Read non-event server metadata, e.g. its version and hostname."""
        result = self._get("info")
        if not isinstance(result, dict):
            raise ActivityWatchError("ActivityWatch returned invalid server metadata")
        return result

    def get_buckets(self) -> dict:
        """Return the server's mapping from bucket ID to bucket metadata."""
        result = self._get("buckets/")
        if not isinstance(result, dict) or any(
            not isinstance(key, str) or not isinstance(value, dict)
            for key, value in result.items()
        ):
            raise ActivityWatchError("ActivityWatch returned invalid bucket metadata")
        return result

    def get_events(
        self,
        bucket_id: str,
        start: datetime | str,
        end: datetime | str,
        limit: int = -1,
    ) -> list[dict]:
        """Fetch bounded raw events locally; ``-1`` means no event-count cap."""
        if not isinstance(bucket_id, str) or not bucket_id or bucket_id in (".", ".."):
            raise ActivityWatchError("ActivityWatch bucket ID is missing")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < -1:
            raise ActivityWatchError("ActivityWatch event limit is invalid")
        start_iso, start_dt = _iso_time(start)
        end_iso, end_dt = _iso_time(end)
        if end_dt <= start_dt:
            raise ActivityWatchError("ActivityWatch query end must follow its start")
        params = {"start": start_iso, "end": end_iso}
        # Both servers interpret an omitted limit as unlimited. The Rust API
        # accepts only unsigned limits, so its query cannot contain "-1".
        if limit >= 0:
            params["limit"] = str(limit)
        result = self._get(f"buckets/{quote(bucket_id, safe='')}/events", params)
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise ActivityWatchError("ActivityWatch returned invalid event data")
        return result

    def fetch_events(
        self, start: datetime | str, end: datetime | str
    ) -> dict[str, list[dict]]:
        """Read the required window/AFK and optional browser watcher buckets.

        Multiple window or AFK sources are ambiguous for a single-device
        report. Fail closed rather than accidentally combining hosts or
        reporting missing activity as zero. Browser buckets may be multiple
        (one per browser); every browser event keeps its source bucket ID.
        """
        hostname, device_id = _server_identity(self.get_info())
        _require_local_server(hostname)
        buckets = self.get_buckets()
        by_type: dict[str, list[str]] = {
            "currentwindow": [],
            "afkstatus": [],
            "web.tab.current": [],
        }
        for bucket_id, metadata in buckets.items():
            event_type = metadata.get("type")
            if isinstance(event_type, str) and event_type in by_type and _is_local_bucket(
                metadata, hostname, device_id
            ):
                by_type[event_type].append(bucket_id)

        if len(by_type["currentwindow"]) != 1:
            raise ActivityWatchError("Expected exactly one local ActivityWatch window watcher bucket")
        if len(by_type["afkstatus"]) != 1:
            raise ActivityWatchError("Expected exactly one local ActivityWatch AFK watcher bucket")

        window = self.get_events(by_type["currentwindow"][0], start, end)
        afk = self.get_events(by_type["afkstatus"][0], start, end)
        if not window or not afk:
            raise ActivityWatchError("ActivityWatch window or AFK events are unavailable")
        web: list[dict] = []
        for bucket_id in sorted(by_type["web.tab.current"]):
            for event in self.get_events(bucket_id, start, end):
                web.append({**event, "bucket_id": bucket_id})
        return {"window": window, "afk": afk, "web": web}
