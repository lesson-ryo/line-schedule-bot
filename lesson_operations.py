"""Shared teacher dashboard, schedule safety checks, and reminder helpers."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
import hashlib
import json
import os
import re
from zoneinfo import ZoneInfo

import requests
from flask import g, has_request_context

from storage import load_json, save_json
from tenant_config import TENANT_NAMES, reset_tenant_override, set_tenant_override


JST = ZoneInfo("Asia/Tokyo")
TENANT_LABELS = {"kansai": "関西", "kanto": "関東"}


@contextmanager
def tenant_scope(name: str):
    """Temporarily select a tenant while keeping shared admin routes tenant-free."""
    if name not in TENANT_NAMES:
        raise ValueError("地域が正しくありません。")
    if not has_request_context():
        token = set_tenant_override(name)
        try:
            yield
        finally:
            reset_tenant_override(token)
        return
    previous = getattr(g, "tenant", None)
    g.tenant = name
    try:
        yield
    finally:
        g.tenant = previous


def _as_list(value):
    return value if isinstance(value, list) else []


def _as_dict(value):
    return value if isinstance(value, dict) else {}


def parse_schedule_date(label: str, today: date | None = None) -> date | None:
    """Read either YYYY-MM-DD or the app's M/D(曜) schedule labels."""
    text = str(label or "").strip()
    today = today or datetime.now(JST).date()
    full = re.search(r"(?<!\d)(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?!\d)", text)
    try:
        if full:
            return date(int(full.group(1)), int(full.group(2)), int(full.group(3)))
        short = re.search(r"(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)", text)
        if not short:
            return None
        value = date(today.year, int(short.group(1)), int(short.group(2)))
        # Around New Year, a December label viewed in January is usually the past
        # schedule, while a January label viewed in December is the next schedule.
        if value < today - timedelta(days=180):
            value = date(today.year + 1, value.month, value.day)
        elif value > today + timedelta(days=180):
            value = date(today.year - 1, value.month, value.day)
        return value
    except ValueError:
        return None


def _clock(value: str) -> time | None:
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(value or ""))
    if not match:
        return None
    try:
        return time(int(match.group(1)), int(match.group(2)))
    except ValueError:
        return None


def _slot_interval(slot: dict, today: date | None = None):
    day = parse_schedule_date(slot.get("day", ""), today=today)
    start = _clock(slot.get("time", ""))
    end = _clock(slot.get("end", ""))
    if not day or not start or not end:
        return None
    return datetime.combine(day, start, JST), datetime.combine(day, end, JST)


def _calendar_events(tenant: str, assignment: list[dict], now: datetime | None = None) -> list[dict]:
    """Convert current/future assignment rows into stable Calendar events."""
    now = now or datetime.now(JST)
    label = TENANT_LABELS[tenant]
    occurrences = {}
    events = []
    for slot in assignment:
        interval = _slot_interval(slot, today=now.date())
        if not interval or interval[1] < now:
            continue
        name = str(slot.get("name", "") or "レッスン").strip()
        location = str(slot.get("location", "") or "").strip()
        identity = json.dumps(
            {
                "tenant": tenant,
                "start": interval[0].isoformat(),
                "end": interval[1].isoformat(),
                "name": name,
                "location": location,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        occurrences[identity] = occurrences.get(identity, 0) + 1
        key = hashlib.sha256(
            f"{identity}:{occurrences[identity]}".encode("utf-8")
        ).hexdigest()[:32]
        description = "\n".join(
            [
                "日程調整アプリから自動同期",
                f"地域: {label}",
                f"生徒: {name}",
            ]
        )
        events.append(
            {
                "key": key,
                "title": f"【{label}】レッスン：{name}",
                "start": interval[0].isoformat(),
                "end": interval[1].isoformat(),
                "location": location,
                "description": description,
            }
        )
    return events


def calendar_sync_status(tenant: str) -> dict:
    """Return a secret-free, no-write preview of one region's Calendar state."""
    if tenant not in TENANT_NAMES:
        raise ValueError("地域が正しくありません。")
    with tenant_scope(tenant):
        assignment = _as_list(load_json("assignment", default=[]))
        state = _as_dict(load_json("calendar_sync", default={}))
    events = _calendar_events(tenant, assignment)
    records = _as_list(state.get("records"))
    synced = {str(row.get("key") or "") for row in records if isinstance(row, dict)}
    return {
        "tenant": tenant,
        "label": TENANT_LABELS[tenant],
        "configured": bool(
            os.environ.get("REPERTOIRE_SHEET_WRITE_URL", "").strip()
            and os.environ.get("REPERTOIRE_SHEET_WRITE_SECRET", "").strip()
        ),
        "calendar_name": str(state.get("calendar_name") or f"Lesson {TENANT_LABELS[tenant]} 日程"),
        "event_count": len(events),
        "synced_count": sum(event["key"] in synced for event in events),
        "pending_count": sum(event["key"] not in synced for event in events),
        "last_synced_at": str(state.get("synced_at") or ""),
        "last_error": str(state.get("last_error") or ""),
        "events": events,
    }


def sync_calendar_schedule(tenant: str) -> dict:
    """Mirror one region's current/future assignments to its Google Calendar."""
    status = calendar_sync_status(tenant)
    if not status["configured"]:
        return {
            "ok": False,
            "configured": False,
            "error": "Googleカレンダー連携がまだ設定されていません。",
            "created": 0,
            "updated": 0,
            "deleted": 0,
        }

    with tenant_scope(tenant):
        state = _as_dict(load_json("calendar_sync", default={}))
    old_records = [row for row in _as_list(state.get("records")) if isinstance(row, dict)]
    desired_keys = {event["key"] for event in status["events"]}
    now = datetime.now(JST)
    keep_records = []
    delete_event_ids = []
    for record in old_records:
        if str(record.get("key") or "") in desired_keys:
            continue
        try:
            start = datetime.fromisoformat(str(record.get("start") or ""))
        except ValueError:
            start = now
        if start.tzinfo is None:
            start = start.replace(tzinfo=JST)
        if start < now:
            keep_records.append(record)
        elif record.get("event_id"):
            delete_event_ids.append(str(record["event_id"]))

    payload = {
        "secret": os.environ.get("REPERTOIRE_SHEET_WRITE_SECRET", ""),
        "action": "calendar_sync",
        "tenant": tenant,
        "events": status["events"],
        "existing": old_records,
        "delete_event_ids": delete_event_ids,
    }
    try:
        response = requests.post(
            os.environ["REPERTOIRE_SHEET_WRITE_URL"], json=payload, timeout=60
        )
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or "Googleカレンダーの同期に失敗しました。")
        new_state = {
            "version": 1,
            "calendar_id": str(result.get("calendar_id") or ""),
            "calendar_name": str(result.get("calendar_name") or status["calendar_name"]),
            "synced_at": datetime.now(JST).isoformat(),
            "last_error": "",
            "records": (keep_records + _as_list(result.get("records")))[-1000:],
        }
        with tenant_scope(tenant):
            save_json("calendar_sync", new_state)
        return {
            "ok": True,
            "configured": True,
            "calendar_name": new_state["calendar_name"],
            "created": int(result.get("created") or 0),
            "updated": int(result.get("updated") or 0),
            "deleted": int(result.get("deleted") or 0),
            "error": "",
        }
    except Exception as exc:
        state["last_error"] = str(exc)[:300]
        with tenant_scope(tenant):
            save_json("calendar_sync", state)
        return {
            "ok": False,
            "configured": True,
            "error": str(exc)[:300],
            "created": 0,
            "updated": 0,
            "deleted": 0,
        }


def cross_tenant_conflicts(current_tenant: str, proposed: list[dict]) -> list[str]:
    """Return teacher-time overlaps against the other regional schedule."""
    conflicts = []
    today = datetime.now(JST).date()
    for other in TENANT_NAMES:
        if other == current_tenant:
            continue
        with tenant_scope(other):
            existing = _as_list(load_json("assignment", default=[]))
        for ours in proposed:
            a = _slot_interval(ours, today=today)
            if not a:
                continue
            for theirs in existing:
                b = _slot_interval(theirs, today=today)
                if not b or a[0].date() != b[0].date():
                    continue
                if a[0] < b[1] and b[0] < a[1]:
                    conflicts.append(
                        f"{ours.get('day', '')} {ours.get('time', '')}-{ours.get('end', '')} "
                        f"は{TENANT_LABELS[other]}の{theirs.get('time', '')}-{theirs.get('end', '')} "
                        f"（{theirs.get('name', '予定あり')}）と重なります。"
                    )
    return conflicts


def schedule_summary(tenant: str) -> dict:
    with tenant_scope(tenant):
        members = _as_list(load_json("members", default=[]))
        votes = _as_list(load_json("votes", default=[]))
        skips = _as_list(load_json("skips", default=[]))
        candidates = _as_list(load_json("candidates", default=[]))
        assignment = _as_list(load_json("assignment", default=[]))
        reminders = _as_list(load_json("reminders", default=[]))
        schedule_targets = _as_list(load_json("schedule_targets", default=[]))
        deadline = str(load_json("deadline", default="") or "")
    if schedule_targets:
        wanted = {str(value) for value in schedule_targets}
        members = [m for m in members if str(m.get("user_id") or "") in wanted]
    responded = {str(row.get("user_id") or "") for row in votes}
    responded.update(str(value) for value in skips)
    nonrespondents = [m for m in members if str(m.get("user_id") or "") not in responded]
    return {
        "tenant": tenant,
        "label": TENANT_LABELS[tenant],
        "members": len(members),
        "responded": len(members) - len(nonrespondents),
        "nonrespondents": len(nonrespondents),
        "candidate_count": len(candidates),
        "assignment_count": len(assignment),
        "deadline": deadline,
        "last_reminder_at": reminders[-1].get("sent_at", "") if reminders else "",
    }


def dashboard_data() -> dict:
    schedules = {name: schedule_summary(name) for name in TENANT_NAMES}
    upcoming = []
    today = datetime.now(JST).date()
    for tenant in TENANT_NAMES:
        with tenant_scope(tenant):
            assignment = _as_list(load_json("assignment", default=[]))
        for slot in assignment:
            when = _slot_interval(slot, today=today)
            if not when or when[0].date() < today:
                continue
            upcoming.append(
                {
                    "tenant": tenant,
                    "label": TENANT_LABELS[tenant],
                    "day": slot.get("day", ""),
                    "time": slot.get("time", ""),
                    "end": slot.get("end", ""),
                    "name": slot.get("name", ""),
                    "sort": when[0],
                }
            )
    upcoming.sort(key=lambda item: item["sort"])
    for item in upcoming:
        item.pop("sort", None)

    with tenant_scope("kanto"):
        carte_members = _as_list(load_json("carte:members", default=[]))
        progress = _as_list(load_json("carte:progress", default=[]))
        requests = _as_list(load_json("carte:requests", default=[]))
    carte = {
        "students": len(carte_members),
        "progress": len(progress),
        "completed": sum(bool(row.get("lesson_done")) or row.get("status") == "completed" for row in progress),
        "wanted": sum(row.get("status") == "wanted" for row in progress),
        "next_students": len({row.get("user_id") for row in progress if row.get("next_lesson")}),
        "open_requests": sum((row.get("status") or "open") == "open" for row in requests),
    }
    return {"schedules": schedules, "carte": carte, "upcoming": upcoming[:12]}


def reminder_preview(tenant: str, kind: str, today: date | None = None) -> dict:
    """Build a no-send preview for nonrespondents or tomorrow's lessons."""
    today = today or datetime.now(JST).date()
    with tenant_scope(tenant):
        members = _as_list(load_json("members", default=[]))
        votes = _as_list(load_json("votes", default=[]))
        skips = _as_list(load_json("skips", default=[]))
        deadline = str(load_json("deadline", default="") or "")
        assignment = _as_list(load_json("assignment", default=[]))
        schedule_targets = _as_list(load_json("schedule_targets", default=[]))

    if schedule_targets:
        wanted = {str(value) for value in schedule_targets}
        members = [m for m in members if str(m.get("user_id") or "") in wanted]

    if kind == "unanswered":
        responded = {str(row.get("user_id") or "") for row in votes}
        responded.update(str(value) for value in skips)
        targets = [m for m in members if str(m.get("user_id") or "") not in responded]
        lines = ["日程調整のご回答がまだ確認できていません。", "お手数ですが、先にお送りした「日程を選ぶ」ボタンからご回答をお願いします。"]
        if deadline:
            lines.append(f"回答期限: {deadline}")
        return {
            "kind": kind,
            "label": "未回答者リマインド",
            "targets": [
                {"user_id": row.get("user_id", ""), "display_name": row.get("display_name", ""), "text": "\n\n".join(lines)}
                for row in targets
                if row.get("user_id")
            ],
        }

    if kind != "tomorrow":
        raise ValueError("リマインド種別が正しくありません。")
    target_day = today + timedelta(days=1)
    member_names = {m.get("user_id"): m.get("display_name", "") for m in members}
    targets = []
    seen = set()
    for slot in assignment:
        if parse_schedule_date(slot.get("day", ""), today=today) != target_day:
            continue
        when = f"{slot.get('day', '')} {slot.get('time', '')}〜{slot.get('end', '')}".strip()
        text = f"明日のレッスン確認です。\n\n■ {when}"
        if slot.get("location"):
            text += f"\n教室: {slot.get('location')}"
        text += "\n\nよろしくお願いします。"
        for user_id in slot.get("member_ids", []):
            if not user_id or user_id in seen:
                continue
            seen.add(user_id)
            targets.append(
                {"user_id": user_id, "display_name": member_names.get(user_id, slot.get("name", "")), "text": text}
            )
    return {"kind": kind, "label": "前日確認", "targets": targets}


def send_reminders(tenant: str, kind: str, push_text, today: date | None = None) -> dict:
    preview = reminder_preview(tenant, kind, today=today)
    sent = []
    error = ""
    with tenant_scope(tenant):
        for target in preview["targets"]:
            try:
                push_text(target["user_id"], target["text"])
                sent.append(target)
            except Exception as exc:
                error = f"{target.get('display_name') or '送信先'}への送信で止まりました: {str(exc)[:180]}"
                break
        history = _as_list(load_json("reminders", default=[]))
        history.append(
            {
                "kind": kind,
                "sent_at": datetime.now(JST).isoformat(),
                "user_ids": [item["user_id"] for item in sent],
                "count": len(sent),
                "error": error,
            }
        )
        save_json("reminders", history[-500:])
    return {
        "ok": not error,
        "count": len(sent),
        "targets": sent,
        "label": preview["label"],
        "error": error,
    }
