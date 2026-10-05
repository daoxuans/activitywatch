"""Visible, opt-in command-line controls for one Windows ActivityWatch client."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .activitywatch import ActivityWatchClient, ActivityWatchError
from .aggregate import DataUnavailable
from .client import preview_local_only, run_once
from .state import SharingState, StateError, canonical_endpoint
from .upload import AuthenticationError, SummaryUploader, UploadError


def _default_state_file() -> Path:
    local_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_data) if local_data else Path.home() / ".local" / "state"
    return base / "ActivityWatchSummaryShare" / "state.json"


def _utc_offset(value: str) -> timezone:
    match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", value)
    if not match:
        raise ValueError("时区偏移须为 +08:00 这样的格式")
    hours, minutes = int(match[2]), int(match[3])
    if hours > 14 or minutes > 59 or hours == 14 and minutes != 0:
        raise ValueError("时区偏移超出允许范围")
    delta = timedelta(hours=hours, minutes=minutes)
    return timezone(delta if match[1] == "+" else -delta)


def _day(value: str, now: datetime) -> date:
    if value == "today":
        return now.date()
    if value == "yesterday":
        return now.date() - timedelta(days=1)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError("日期须为 today、yesterday 或 YYYY-MM-DD") from None


def _categories(
    path: Path | None,
) -> tuple[dict[str, str], dict[str, str], dict[str, list[str]]]:
    if path is None:
        return {}, {}, {}
    if not path.exists():
        raise ValueError("指定的分类文件不存在")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("分类文件无法读取或不是有效 JSON") from None
    if not isinstance(document, dict):
        raise ValueError("分类文件必须是对象")
    result: list[dict[str, str]] = []
    for field in ("apps", "domains"):
        rules = document.get(field, {})
        if not isinstance(rules, dict) or any(
            not isinstance(name, str)
            or not name
            or len(name) > 253
            or not isinstance(category, str)
            or not category.strip()
            or len(category) > 120
            for name, category in rules.items()
        ):
            raise ValueError("分类文件的 apps/domains 须为名称到类别的文本映射")
        result.append(rules)
    browsers = document.get("browsers", {})
    if not isinstance(browsers, dict) or any(
        not isinstance(watcher, str)
        or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", watcher)
        or not isinstance(apps, list)
        or not apps
        or any(not isinstance(app, str) or not app or len(app) > 120 for app in apps)
        for watcher, apps in browsers.items()
    ):
        raise ValueError("分类文件的 browsers 须为浏览器扩展名到软件文件名列表的映射")
    return result[0], result[1], browsers


def _endpoint(args: argparse.Namespace) -> str:
    value = args.endpoint or os.environ.get("AW_SHARE_ENDPOINT")
    if not value:
        raise ValueError("请先设置 HTTPS 接收地址：AW_SHARE_ENDPOINT")
    return canonical_endpoint(value)


def _uploader(args: argparse.Namespace) -> SummaryUploader:
    token = os.environ.get("AW_SHARE_API_TOKEN")
    if not token:
        raise ValueError("请先设置本机环境变量 AW_SHARE_API_TOKEN（不要写入命令参数）")
    return SummaryUploader(_endpoint(args), token=token)


def _aw_client(args: argparse.Namespace) -> ActivityWatchClient:
    return ActivityWatchClient(
        args.aw_url,
        api_key=os.environ.get("AW_SHARE_AW_API_KEY"),
    )


def _send_one(
    chosen_day: date,
    now: datetime,
    zone: timezone,
    args: argparse.Namespace,
    state: SharingState,
    *,
    upload: bool,
) -> dict[str, Any] | None:
    state.refresh()
    if not state.enabled:
        return None
    apps, domains, browsers = _categories(args.categories)
    return run_once(
        day=chosen_day,
        now=now,
        timezone=zone,
        aw_client=_aw_client(args),
        state=state,
        uploader=_uploader(args) if upload else None,
        app_categories=apps,
        domain_categories=domains,
        browser_apps=browsers,
        upload=upload,
    )


def _watch(args: argparse.Namespace, state: SharingState, zone: timezone) -> int:
    if args.interval < 60 or args.interval > 86400:
        raise ValueError("轮询间隔须在 60 到 86400 秒之间")
    if args.catch_up_days < 0 or args.catch_up_days > 30:
        raise ValueError("日报补发天数须在 0 到 30 之间")
    # Validate endpoint and credentials before beginning a long-running loop.
    _uploader(args)
    print("汇总分享正在运行。按 Ctrl+C 停止本客户端；ActivityWatch 本机记录不受影响。")
    try:
        while True:
            now = datetime.now(zone)
            state.refresh()
            if not state.enabled:
                print("分享已暂停，后台上报结束。")
                return 0
            for days_ago in range(args.catch_up_days, 0, -1):
                candidate = now.date() - timedelta(days=days_ago)
                # Give the local watcher time to flush events around midnight.
                if days_ago == 1 and (now.hour, now.minute) < (0, 15):
                    continue
                if state.has_sent_day(candidate, kind="daily"):
                    continue
                try:
                    _send_one(candidate, now, zone, args, state, upload=True)
                except AuthenticationError:
                    raise
                except (ActivityWatchError, DataUnavailable, UploadError) as exc:
                    print(f"{candidate.isoformat()} 日报暂未送达：{exc}", file=sys.stderr)
            try:
                _send_one(now.date(), now, zone, args, state, upload=True)
            except AuthenticationError:
                raise
            except (ActivityWatchError, DataUnavailable, UploadError) as exc:
                print(f"当天快照暂未送达：{exc}", file=sys.stderr)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("已停止本客户端上报。")
        return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m aw_share",
        description="本机 ActivityWatch 汇总分享；仅在明确启用后发送软件、域名与分类时长",
    )
    parser.add_argument("--state-file", type=Path, default=_default_state_file())
    parser.add_argument("--endpoint", help="HTTPS 汇总接收地址；也可设 AW_SHARE_ENDPOINT")
    parser.add_argument(
        "--aw-url",
        default=os.environ.get("AW_SHARE_AW_URL", "http://127.0.0.1:5600/api/0"),
        help="本机 ActivityWatch API（仅支持回环地址）",
    )
    parser.add_argument("--categories", type=Path, help="本机分类规则 JSON")
    parser.add_argument("--utc-offset", default="+08:00", help="日报日界线，默认北京时间 +08:00")
    actions = parser.add_subparsers(dest="command", required=True)
    actions.add_parser("status", help="查看本机分享状态")
    actions.add_parser("enable", help="本机使用者确认后开启汇总分享")
    actions.add_parser("pause", help="暂停本客户端分享；不停止 ActivityWatch 本机记录")
    actions.add_parser("revoke", help="撤回本客户端分享并清除本地授权历史")
    preview = actions.add_parser("preview", help="只在本机查看已授权时间段的汇总")
    preview.add_argument("--day", default="today")
    local_preview = actions.add_parser("local-preview", help="本机确认后预览活动，不开启分享")
    local_preview.add_argument("--day", default="today")
    send = actions.add_parser("send", help="立即发送一次汇总")
    send.add_argument("--day", default="today")
    watch = actions.add_parser("watch", help="持续发送当天快照并补发尚未送达的日报")
    watch.add_argument("--interval", type=int, default=900, help="快照间隔秒数，默认 900")
    watch.add_argument("--catch-up-days", type=int, default=7, help="最多补发几天日报，默认 7")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        zone = _utc_offset(args.utc_offset)
        now = datetime.now(zone)
        state = SharingState.load(args.state_file)
        if args.command == "status":
            state.refresh()
            print("分享状态：" + ("已开启" if state.enabled else "已暂停/未开启"))
            print("已确认的接收地址：" + (state.approved_endpoint or "无"))
            print("设备标识：" + (state.device_id if state.path.exists() else "启用时生成"))
            print("今天曾有快照送达：" + ("是" if state.has_sent_day(now.date(), "current") else "否"))
            yesterday = now.date() - timedelta(days=1)
            print("昨天日报已送达：" + ("是" if state.has_sent_day(yesterday, "daily") else "否"))
            print("注意：此状态仅控制汇总分享，不控制 ActivityWatch 本机记录。")
        elif args.command == "enable":
            endpoint = _endpoint(args)
            print("即将发送：类别及总时长、软件名称及各自时长、网站域名及各自时长。")
            print("不会发送：窗口标题、完整网址、文件路径或逐条活动。")
            print("接收地址：" + endpoint)
            print("暂停分享不会停止 ActivityWatch 自身在本机的记录。")
            try:
                confirmation = input("本机使用者知情并同意，输入“我同意分享”以开启：").strip()
            except (EOFError, KeyboardInterrupt):
                confirmation = ""
            if confirmation != "我同意分享":
                print("未开启。")
                return 1
            state.enable(at=datetime.now(zone), endpoint=endpoint)
            print("已开启。使用 watch 才会持续自动上报。")
        elif args.command == "pause":
            state.pause(at=datetime.now(zone))
            print("已暂停本客户端分享；ActivityWatch 仍可能在本机记录。")
        elif args.command == "revoke":
            state.revoke(at=datetime.now(zone))
            print("已撤回本客户端授权；云端已接收数据的删除需由接收端另行执行。")
        elif args.command == "watch":
            return _watch(args, state, zone)
        elif args.command == "local-preview":
            print("将读取本机 ActivityWatch 活动，仅在此终端显示汇总；不保存授权或上报。")
            try:
                confirmation = input("本机使用者输入“我同意本机预览”继续：").strip()
            except (EOFError, KeyboardInterrupt):
                confirmation = ""
            if confirmation != "我同意本机预览":
                print("未读取本机活动。")
                return 1
            apps, domains, browsers = _categories(args.categories)
            report = preview_local_only(
                day=_day(args.day, now),
                now=now,
                timezone=zone,
                aw_client=_aw_client(args),
                app_categories=apps,
                domain_categories=domains,
                browser_apps=browsers,
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            chosen_day = _day(args.day, now)
            report = _send_one(
                chosen_day, now, zone, args, state, upload=args.command == "send"
            )
            if report is None:
                print("分享未开启，或这一天没有获准分享的时间段；未读取和发送活动记录。")
            elif args.command == "preview":
                print(json.dumps(report, ensure_ascii=False, indent=2))
            else:
                print(f"汇总已送达：{report['day']}，累计 {report['total_seconds']} 秒。")
        return 0
    except (ActivityWatchError, DataUnavailable, UploadError, StateError, ValueError) as exc:
        print(f"操作未完成：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
