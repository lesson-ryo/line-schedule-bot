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
from tenant_config import TENANTS, TENANT_NAMES, reset_tenant_override, set_tenant_override


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


def sync_calendar_schedule(tenant: str, sync_attendance: bool = True) -> dict:
    """Mirror one region's current/future assignments to its Google Calendar."""
    if sync_attendance:
        sync_attendance_from_schedule(tenant)
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


def _lesson_identity(tenant: str, slot: dict, occurrence: int = 1) -> str:
    """Stable-enough ID for older assignment rows that predate lesson IDs."""
    raw = json.dumps(
        {
            "tenant": tenant,
            "day": slot.get("day", ""),
            "time": slot.get("time", ""),
            "end": slot.get("end", ""),
            "name": slot.get("name", ""),
            "member_ids": sorted(str(v) for v in slot.get("member_ids", []) if v),
            "occurrence": occurrence,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def ensure_lesson_ids(tenant: str) -> list[dict]:
    """Add internal IDs to existing rows without changing visible schedule data."""
    with tenant_scope(tenant):
        assignment = _as_list(load_json("assignment", default=[]))
    changed = False
    seen = {}
    for slot in assignment:
        base = json.dumps(
            {key: slot.get(key) for key in ("day", "time", "end", "name")},
            ensure_ascii=False,
            sort_keys=True,
        )
        seen[base] = seen.get(base, 0) + 1
        if not slot.get("lesson_id"):
            slot["lesson_id"] = _lesson_identity(tenant, slot, seen[base])
            changed = True
    if changed:
        with tenant_scope(tenant):
            save_json("assignment", assignment)
    return assignment


def _shared_members() -> dict[str, str]:
    with tenant_scope("kanto"):
        members = _as_list(load_json("carte:members", default=[]))
    return {
        str(row.get("user_id") or ""): str(row.get("display_name") or "")
        for row in members
        if row.get("user_id")
    }


def sync_attendance_from_schedule(tenant: str) -> dict:
    """Create one shared attendance row per student and scheduled lesson."""
    assignment = ensure_lesson_ids(tenant)
    with tenant_scope(tenant):
        regional_members = _as_list(load_json("members", default=[]))
    names = _shared_members()
    names.update(
        {
            str(row.get("user_id") or ""): str(row.get("display_name") or "")
            for row in regional_members
            if row.get("user_id")
        }
    )
    with tenant_scope("kanto"):
        rows = _as_list(load_json("carte:attendance", default=[]))
    by_id = {str(row.get("id") or ""): row for row in rows if isinstance(row, dict)}
    created = 0
    updated = 0
    now = datetime.now(JST).isoformat()
    active_record_ids = set()
    for slot in assignment:
        lesson_id = str(slot.get("lesson_id") or "")
        for user_id in [str(value) for value in slot.get("member_ids", []) if value]:
            record_id = f"{tenant}:{lesson_id}:{user_id}"
            active_record_ids.add(record_id)
            values = {
                "id": record_id,
                "lesson_id": lesson_id,
                "tenant": tenant,
                "user_id": user_id,
                "display_name": names.get(user_id) or str(slot.get("name") or ""),
                "day": str(slot.get("day") or ""),
                "time": str(slot.get("time") or ""),
                "end": str(slot.get("end") or ""),
                "location": str(slot.get("location") or ""),
            }
            if record_id not in by_id:
                values.update(status="scheduled", note="", created_at=now, updated_at=now)
                rows.append(values)
                by_id[record_id] = values
                created += 1
            else:
                record = by_id[record_id]
                if any(record.get(key) != value for key, value in values.items()):
                    record.update(values, updated_at=now)
                    updated += 1
    today = datetime.now(JST).date()
    for record in rows:
        if record.get("tenant") != tenant or record.get("id") in active_record_ids:
            continue
        lesson_day = parse_schedule_date(record.get("day", ""), today=today)
        if record.get("status", "scheduled") == "scheduled" and lesson_day and lesson_day >= today:
            record.update(status="cancelled", updated_at=now)
            updated += 1
    if created or updated:
        with tenant_scope("kanto"):
            save_json("carte:attendance", rows[-5000:])
    return {"created": created, "updated": updated}


def mark_lesson_attendance(tenant: str, lesson_id: str, status: str) -> None:
    with tenant_scope("kanto"):
        rows = _as_list(load_json("carte:attendance", default=[]))
    changed = False
    now = datetime.now(JST).isoformat()
    for row in rows:
        if row.get("tenant") == tenant and row.get("lesson_id") == lesson_id:
            row["status"] = status
            row["updated_at"] = now
            changed = True
    if changed:
        with tenant_scope("kanto"):
            save_json("carte:attendance", rows)


def lessons_data(now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(JST)
    rows = []
    for tenant in TENANT_NAMES:
        sync_attendance_from_schedule(tenant)
        for slot in ensure_lesson_ids(tenant):
            interval = _slot_interval(slot, today=now.date())
            if not interval or interval[1] < now:
                continue
            rows.append(
                {
                    **slot,
                    "tenant": tenant,
                    "label": TENANT_LABELS[tenant],
                    "date_value": interval[0].date().isoformat(),
                    "sort": interval[0],
                }
            )
    rows.sort(key=lambda row: row["sort"])
    for row in rows:
        row.pop("sort", None)
    return rows


def _same_tenant_conflicts(proposed: list[dict], today: date | None = None) -> list[str]:
    today = today or datetime.now(JST).date()
    conflicts = []
    for index, left in enumerate(proposed):
        a = _slot_interval(left, today=today)
        if not a:
            continue
        for right in proposed[index + 1 :]:
            b = _slot_interval(right, today=today)
            if not b or a[0].date() != b[0].date():
                continue
            if a[0] < b[1] and b[0] < a[1]:
                conflicts.append(
                    f"{left.get('day', '')} {left.get('time', '')}-{left.get('end', '')} "
                    f"は同じ地域の{right.get('time', '')}-{right.get('end', '')}と重なります。"
                )
    return conflicts


def update_lesson(
    tenant: str,
    lesson_id: str,
    values: dict,
    push_text=None,
    notify: bool = True,
) -> dict:
    """Reschedule one lesson, keep attendance linked, sync Calendar, then notify."""
    if tenant not in TENANT_NAMES:
        raise ValueError("地域が正しくありません。")
    assignment = ensure_lesson_ids(tenant)
    index = next((i for i, row in enumerate(assignment) if row.get("lesson_id") == lesson_id), -1)
    if index < 0:
        raise ValueError("レッスンが見つかりません。画面を読み込み直してください。")
    old = dict(assignment[index])
    day = str(values.get("day") or "").strip()
    start = str(values.get("time") or "").strip()
    end = str(values.get("end") or "").strip()
    candidate = {**old, "day": day, "time": start, "end": end, "location": str(values.get("location") or "").strip()[:120]}
    interval = _slot_interval(candidate)
    if not interval or interval[1] <= interval[0]:
        raise ValueError("日付・開始時刻・終了時刻を正しく入力してください。")
    proposed = [candidate if i == index else row for i, row in enumerate(assignment)]
    conflicts = _same_tenant_conflicts(proposed) + cross_tenant_conflicts(tenant, proposed)
    if conflicts:
        raise ValueError(" ".join(conflicts))
    assignment[index] = candidate
    with tenant_scope(tenant):
        save_json("assignment", assignment)
    sync_attendance_from_schedule(tenant)
    calendar = sync_calendar_schedule(tenant, sync_attendance=False)
    sent = 0
    error = ""
    if notify and push_text:
        text = "\n".join(
            [
                "レッスン日時が変更になりました。",
                "",
                f"変更前: {old.get('day', '')} {old.get('time', '')}〜{old.get('end', '')}",
                f"変更後: {candidate['day']} {candidate['time']}〜{candidate['end']}",
                *( [f"教室: {candidate['location']}"] if candidate.get("location") else [] ),
                "",
                "ご確認をお願いします。",
            ]
        )
        with tenant_scope(tenant):
            for user_id in candidate.get("member_ids", []):
                try:
                    push_text(user_id, text)
                    sent += 1
                except Exception as exc:
                    error = str(exc)[:180]
                    break
    return {"ok": not error, "sent": sent, "error": error, "calendar": calendar}


def cancel_lesson(
    tenant: str,
    lesson_id: str,
    push_text=None,
    notify: bool = True,
) -> dict:
    """Cancel one future lesson and retain a cancelled attendance record."""
    assignment = ensure_lesson_ids(tenant)
    slot = next((row for row in assignment if row.get("lesson_id") == lesson_id), None)
    if not slot:
        raise ValueError("レッスンが見つかりません。画面を読み込み直してください。")
    sync_attendance_from_schedule(tenant)
    mark_lesson_attendance(tenant, lesson_id, "cancelled")
    with tenant_scope(tenant):
        save_json("assignment", [row for row in assignment if row.get("lesson_id") != lesson_id])
    calendar = sync_calendar_schedule(tenant, sync_attendance=False)
    sent = 0
    error = ""
    if notify and push_text:
        text = "\n".join(
            [
                "レッスン中止のお知らせです。",
                "",
                f"{slot.get('day', '')} {slot.get('time', '')}〜{slot.get('end', '')}",
                "",
                "この回はキャンセルとなりました。ご確認をお願いします。",
            ]
        )
        with tenant_scope(tenant):
            for user_id in slot.get("member_ids", []):
                try:
                    push_text(user_id, text)
                    sent += 1
                except Exception as exc:
                    error = str(exc)[:180]
                    break
    return {"ok": not error, "sent": sent, "error": error, "calendar": calendar}


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


ATTENDANCE_STATUSES = {
    "scheduled": "予定",
    "attended": "出席",
    "absent": "欠席",
    "cancelled": "キャンセル",
}


def attendance_month_data(month: str = "") -> dict:
    now = datetime.now(JST)
    if not re.fullmatch(r"\d{4}-\d{2}", str(month or "")):
        month = now.strftime("%Y-%m")
    for tenant in TENANT_NAMES:
        sync_attendance_from_schedule(tenant)
    with tenant_scope("kanto"):
        rows = _as_list(load_json("carte:attendance", default=[]))
        tuition = _as_list(load_json("carte:tuition", default=[]))
        members = _as_list(load_json("carte:members", default=[]))
    selected = []
    for row in rows:
        day = parse_schedule_date(row.get("day", ""), today=now.date())
        if day and day.strftime("%Y-%m") == month:
            item = dict(row)
            item["date_value"] = day.isoformat()
            item["label"] = TENANT_LABELS.get(str(row.get("tenant") or ""), "")
            selected.append(item)
    selected.sort(key=lambda row: (row.get("date_value", ""), row.get("time", ""), row.get("display_name", "")))
    names = {
        str(row.get("user_id") or ""): str(row.get("display_name") or "")
        for row in members
        if row.get("user_id")
    }
    for row in selected:
        names.setdefault(str(row.get("user_id") or ""), str(row.get("display_name") or ""))
    ledger = {
        f"{row.get('month')}|{row.get('user_id')}": row
        for row in tuition
        if isinstance(row, dict)
    }
    students = []
    for user_id, display_name in sorted(names.items(), key=lambda item: item[1] or item[0]):
        mine = [row for row in selected if row.get("user_id") == user_id]
        counts = {key: sum(row.get("status", "scheduled") == key for row in mine) for key in ATTENDANCE_STATUSES}
        fee = ledger.get(f"{month}|{user_id}", {})
        students.append(
            {
                "user_id": user_id,
                "display_name": display_name or "名前未登録",
                "counts": counts,
                "amount": int(fee.get("amount") or 0),
                "paid": bool(fee.get("paid")),
                "note": str(fee.get("note") or ""),
            }
        )
    return {
        "month": month,
        "rows": selected,
        "students": students,
        "statuses": ATTENDANCE_STATUSES,
    }


def update_attendance_record(record_id: str, status: str, note: str = "") -> dict:
    if status not in ATTENDANCE_STATUSES:
        raise ValueError("出欠状態が正しくありません。")
    with tenant_scope("kanto"):
        rows = _as_list(load_json("carte:attendance", default=[]))
    row = next((item for item in rows if item.get("id") == record_id), None)
    if not row:
        raise ValueError("出欠記録が見つかりません。画面を読み込み直してください。")
    row.update(status=status, note=str(note or "").strip()[:500], updated_at=datetime.now(JST).isoformat())
    with tenant_scope("kanto"):
        save_json("carte:attendance", rows)
    return row


def save_tuition_record(user_id: str, month: str, amount, paid: bool, note: str = "") -> dict:
    if not user_id or not re.fullmatch(r"\d{4}-\d{2}", str(month or "")):
        raise ValueError("生徒または対象月が正しくありません。")
    try:
        amount = int(str(amount or "0").replace(",", ""))
    except ValueError as exc:
        raise ValueError("月謝は数字で入力してください。") from exc
    if amount < 0 or amount > 10000000:
        raise ValueError("月謝の金額が正しくありません。")
    with tenant_scope("kanto"):
        rows = _as_list(load_json("carte:tuition", default=[]))
    record = next(
        (row for row in rows if row.get("user_id") == user_id and row.get("month") == month),
        None,
    )
    values = {
        "user_id": user_id,
        "month": month,
        "amount": amount,
        "paid": bool(paid),
        "paid_at": datetime.now(JST).isoformat() if paid else "",
        "note": str(note or "").strip()[:500],
        "updated_at": datetime.now(JST).isoformat(),
    }
    if record is None:
        rows.append(values)
        record = values
    else:
        old_paid_at = record.get("paid_at", "")
        record.update(values)
        if paid and old_paid_at:
            record["paid_at"] = old_paid_at
    with tenant_scope("kanto"):
        save_json("carte:tuition", rows[-5000:])
    return record


def automation_settings(tenant: str) -> dict:
    if tenant not in TENANT_NAMES:
        raise ValueError("地域が正しくありません。")
    defaults = {
        "tomorrow_enabled": True,
        "tomorrow_time": "18:00",
        "unanswered_enabled": True,
        "unanswered_time": "10:00",
    }
    with tenant_scope(tenant):
        saved = _as_dict(load_json("automation_settings", default={}))
    return {**defaults, **saved}


def save_automation_settings(tenant: str, values: dict) -> dict:
    settings = automation_settings(tenant)
    for key in ("tomorrow_time", "unanswered_time"):
        value = str(values.get(key) or settings[key]).strip()
        if not _clock(value):
            raise ValueError("通知時刻が正しくありません。")
        settings[key] = value
    settings["tomorrow_enabled"] = bool(values.get("tomorrow_enabled"))
    settings["unanswered_enabled"] = bool(values.get("unanswered_enabled"))
    with tenant_scope(tenant):
        save_json("automation_settings", settings)
    return settings


def _parse_deadline(value: str, today: date) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=JST) if parsed.tzinfo is None else parsed.astimezone(JST)
    except ValueError:
        pass
    day = parse_schedule_date(text, today=today)
    match = re.search(r"(\d{1,2}):(\d{2})", text)
    if not day:
        return None
    clock = time(int(match.group(1)), int(match.group(2))) if match else time(23, 59)
    return datetime.combine(day, clock, JST)


def automation_status(tenant: str) -> dict:
    settings = automation_settings(tenant)
    with tenant_scope(tenant):
        runs = _as_dict(load_json("automation_runs", default={}))
    recent = sorted(
        [value for value in runs.values() if isinstance(value, dict)],
        key=lambda row: str(row.get("completed_at") or row.get("started_at") or ""),
        reverse=True,
    )
    return {
        "tenant": tenant,
        "label": TENANT_LABELS[tenant],
        "settings": settings,
        "last_run": recent[0] if recent else {},
    }


def _run_automation_cycle(tenant: str, kind: str, cycle: str, push_text, now: datetime) -> dict:
    with tenant_scope(tenant):
        runs = _as_dict(load_json("automation_runs", default={}))
    key = f"{kind}:{cycle}"
    state = _as_dict(runs.get(key))
    preview = reminder_preview(tenant, kind, today=now.date())
    target_ids = {
        str(target.get("user_id") or "")
        for target in preview["targets"]
        if target.get("user_id")
    }
    if state.get("completed_at") and target_ids.issubset(
        {str(value) for value in state.get("sent_user_ids", [])}
    ):
        return {"kind": kind, "count": 0, "skipped": "completed"}
    started = str(state.get("started_at") or "")
    if started and not state.get("completed_at"):
        try:
            if now - datetime.fromisoformat(started) < timedelta(minutes=10):
                return {"kind": kind, "count": 0, "skipped": "running"}
        except ValueError:
            pass
    state.update(kind=kind, cycle=cycle, started_at=now.isoformat(), last_error="")
    state.setdefault("sent_user_ids", [])
    runs[key] = state
    with tenant_scope(tenant):
        save_json("automation_runs", dict(list(runs.items())[-200:]))
    sent_ids = set(str(value) for value in state.get("sent_user_ids", []))
    count = 0
    with tenant_scope(tenant):
        for target in preview["targets"]:
            user_id = str(target.get("user_id") or "")
            if not user_id or user_id in sent_ids:
                continue
            try:
                push_text(user_id, target["text"])
                sent_ids.add(user_id)
                count += 1
                state["sent_user_ids"] = sorted(sent_ids)
                runs[key] = state
                save_json("automation_runs", dict(list(runs.items())[-200:]))
            except Exception as exc:
                state.update(started_at="", last_error=str(exc)[:180])
                runs[key] = state
                save_json("automation_runs", dict(list(runs.items())[-200:]))
                return {"kind": kind, "count": count, "error": state["last_error"]}
        state.update(completed_at=now.isoformat(), started_at="", count=len(sent_ids), last_error="")
        runs[key] = state
        save_json("automation_runs", dict(list(runs.items())[-200:]))
        history = _as_list(load_json("reminders", default=[]))
        history.append(
            {
                "kind": kind,
                "automatic": True,
                "cycle": cycle,
                "sent_at": now.isoformat(),
                "user_ids": sorted(sent_ids),
                "count": len(sent_ids),
                "error": "",
            }
        )
        save_json("reminders", history[-500:])
    return {"kind": kind, "count": count, "error": ""}


def run_due_automations(push_text, now: datetime | None = None) -> list[dict]:
    """Run due LINE reminders. Safe to call repeatedly from the health check."""
    now = now or datetime.now(JST)
    results = []
    for tenant in TENANT_NAMES:
        if not TENANTS[tenant].schedule_enabled:
            continue
        settings = automation_settings(tenant)
        tomorrow_at = datetime.combine(now.date(), _clock(settings["tomorrow_time"]), JST)
        if settings["tomorrow_enabled"] and now >= tomorrow_at:
            cycle = (now.date() + timedelta(days=1)).isoformat()
            results.append(_run_automation_cycle(tenant, "tomorrow", cycle, push_text, now))
        with tenant_scope(tenant):
            deadline_text = str(load_json("deadline", default="") or "")
        deadline = _parse_deadline(deadline_text, now.date())
        unanswered_at = datetime.combine(now.date(), _clock(settings["unanswered_time"]), JST)
        if (
            settings["unanswered_enabled"]
            and deadline
            and deadline - timedelta(days=1) <= now < deadline
            and now >= unanswered_at
        ):
            cycle = hashlib.sha256(deadline_text.encode("utf-8")).hexdigest()[:16]
            results.append(_run_automation_cycle(tenant, "unanswered", cycle, push_text, now))
    return results
