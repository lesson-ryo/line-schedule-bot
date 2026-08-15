"""生徒別レパートリーカルテ。

教材マスターは既存Googleスプレッドシートから読み取り、生徒別データだけを
既存のstorage.py（本番はUpstash Redis）へ保存する。
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import os
import time
import unicodedata
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import requests
from flask import Blueprint, abort, make_response, redirect, request

from storage import load_json, save_json


SHEET_ID = os.environ.get(
    "REPERTOIRE_SHEET_ID", "1EzfP2Vs0HBOI_V3MS9aSIYZW0pL_wAWecaw1r7C2vX0"
)
SHEET_GID = os.environ.get("REPERTOIRE_SHEET_GID", "0")
SHEET_CSV_URL = os.environ.get(
    "REPERTOIRE_SHEET_CSV_URL",
    f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={SHEET_GID}",
)

_material_cache = {"at": 0.0, "items": []}
# "wanted" = 生徒または講師が「やりたい」と付けた状態。未実施と実施済みの中間。
VALID_STATUSES = {"planned", "wanted", "practicing", "completed", "paused"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def load_materials(force=False, include_inactive=False):
    """シートを教材マスターへ変換。空行と非公開曲は生徒側へ出さない。"""
    if not force and _material_cache["items"] and time.time() - _material_cache["at"] < 300:
        items = _material_cache["items"]
        return items if include_inactive else [item for item in items if item.get("active", True)]
    res = requests.get(SHEET_CSV_URL, timeout=15)
    res.raise_for_status()
    rows = csv.reader(io.StringIO(res.content.decode("utf-8-sig")))
    next(rows, None)
    items = []
    for row in rows:
        # シート列: A=ID B=楽器 C=形態 D=曲名 E=アーティスト F=Youtube G=メモ H=ジャンル I=公開状態
        # I列が空の既存曲は公開扱い。IDは過去カルテとの紐付けなので削除・採番し直しはしない。
        row += [""] * (9 - len(row))
        material_id, instrument, kind, title, artist, video, note, genre, visibility = [
            v.strip() for v in row[:9]
        ]
        if not material_id.isdigit() or not title:
            continue
        active = _normalized_title(visibility) not in {"非公開", "inactive", "archived", "false", "0"}
        items.append(
            {
                "id": int(material_id),
                "instrument": instrument,
                "kind": kind,
                "title": title,
                "artist": artist,
                "video": video,
                "note": note,
                "genre": genre,
                "active": active,
                "source": "sheet",
            }
        )
    custom = load_json("carte:custom_materials", default=[])
    if isinstance(custom, list):
        known_ids = {item["id"] for item in items}
        for item in custom:
            if not isinstance(item, dict) or not str(item.get("id", "")).isdigit():
                continue
            material_id = int(item["id"])
            title = str(item.get("title", "")).strip()
            if not title or material_id in known_ids:
                continue
            items.append(
                {
                    "id": material_id,
                    "instrument": str(item.get("instrument", "")).strip(),
                    "kind": str(item.get("kind", "")).strip(),
                    "title": title,
                    "artist": str(item.get("artist", "")).strip(),
                    "video": str(item.get("video", "")).strip(),
                    "note": str(item.get("note", "")).strip(),
                    "genre": str(item.get("genre", "")).strip(),
                    "active": bool(item.get("active", True)),
                    "source": "custom",
                }
            )
            known_ids.add(material_id)
    items.sort(key=lambda x: x["id"], reverse=True)
    _material_cache.update(at=time.time(), items=items)
    return items if include_inactive else [item for item in items if item.get("active", True)]


def _normalized_title(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(value.split())


def _youtube_video_id(value: str) -> str:
    parsed = urlparse(str(value or "").strip())
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        return parsed.path.strip("/").split("/", 1)[0]
    if host == "youtube.com" or host.endswith(".youtube.com"):
        if parsed.path == "/watch":
            return (parse_qs(parsed.query).get("v") or [""])[0]
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] in {"embed", "shorts", "live"}:
            return parts[1]
    return ""


def add_material(values: dict) -> tuple[dict | None, str, str]:
    """Add a repertoire item through the optional Sheet writer or shared Redis.

    Returns ``(item, source, error)``. The Redis fallback makes the teacher form
    useful immediately; configuring REPERTOIRE_SHEET_WRITE_URL upgrades it to a
    direct Google Sheet write without changing the UI.
    """
    title = str(values.get("title", "")).strip()[:120]
    instrument = str(values.get("instrument", "")).strip()
    kind = str(values.get("kind", "")).strip()
    artist = str(values.get("artist", "")).strip()[:120]
    video = str(values.get("video", "")).strip()[:500]
    note = str(values.get("note", "")).strip()[:500]
    genre = str(values.get("genre", "")).strip()[:80]

    if not title:
        return None, "", "曲名を入力してください。"
    if instrument not in {"", "ウクレレ", "ギター"}:
        return None, "", "楽器が正しくありません。"
    if kind not in {"", "弾き語り", "ソロ弾き", "メロ弾き", "デュオ"}:
        return None, "", "形態が正しくありません。"
    video_id = ""
    if video:
        parsed = urlparse(video)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None, "", "YouTube URLが正しくありません。"
        video_id = _youtube_video_id(video)
        if not video_id:
            return None, "", "YouTube動画のURLを入力してください。"

    materials = load_materials(force=True)
    normalized = _normalized_title(title)
    for item in materials:
        if _normalized_title(item.get("title", "")) == normalized:
            return None, "", f"同じ曲名がすでにあります（ID {item['id']}）。"
        existing_video = str(item.get("video", "")).strip()
        if video and (
            existing_video == video
            or (video_id and _youtube_video_id(existing_video) == video_id)
        ):
            return None, "", f"同じ動画URLがすでにあります（ID {item['id']}）。"

    payload = {
        "action": "add",
        "title": title,
        "instrument": instrument,
        "kind": kind,
        "artist": artist,
        "video": video,
        "note": note,
        "genre": genre,
        "active": True,
    }
    write_url = os.environ.get("REPERTOIRE_SHEET_WRITE_URL", "").strip()
    if write_url:
        payload["secret"] = os.environ.get("REPERTOIRE_SHEET_WRITE_SECRET", "")
        try:
            response = requests.post(write_url, json=payload, timeout=20)
            response.raise_for_status()
            result = response.json()
            if not result.get("ok"):
                raise RuntimeError(result.get("error") or "Google Sheetへの追加に失敗しました。")
            _material_cache.update(at=0.0, items=[])
            item = dict(payload, id=int(result["id"]), source="sheet")
            item.pop("secret", None)
            item.pop("action", None)
            _record_material_history(item["id"], "add", {}, item)
            return item, "sheet", ""
        except Exception as exc:
            return None, "", f"Google Sheetへの追加に失敗しました: {str(exc)[:240]}"

    custom = load_json("carte:custom_materials", default=[])
    if not isinstance(custom, list):
        custom = []
    material_id = max(
        [999999]
        + [int(item.get("id", 0)) for item in custom if str(item.get("id", "")).isdigit()]
    ) + 1
    item = dict(payload, id=material_id, source="custom", created_at=_now())
    item.pop("action", None)
    custom.append(item)
    save_json("carte:custom_materials", custom[-2000:])
    _material_cache.update(at=0.0, items=[])
    _record_material_history(item["id"], "add", {}, item)
    return item, "custom", ""


def _record_material_history(material_id: int, action: str, before: dict, after: dict):
    rows = load_json("carte:material_history", default=[])
    if not isinstance(rows, list):
        rows = []
    rows.append(
        {
            "material_id": material_id,
            "action": action,
            "before": {k: before.get(k) for k in ("title", "artist", "instrument", "kind", "video", "note", "genre", "active")},
            "after": {k: after.get(k) for k in ("title", "artist", "instrument", "kind", "video", "note", "genre", "active")},
            "timestamp": _now(),
        }
    )
    save_json("carte:material_history", rows[-5000:])


def update_material(material_id: int, values: dict | None = None, action: str = "update") -> tuple[dict | None, str]:
    """Edit, archive, or republish a song without ever changing its stable ID."""
    values = values or {}
    materials = load_materials(force=True, include_inactive=True)
    current = next((item for item in materials if item.get("id") == material_id), None)
    if not current:
        return None, "曲が見つかりません。"
    if action not in {"update", "archive", "publish"}:
        return None, "操作が正しくありません。"

    updated = dict(current)
    if action == "update":
        title = str(values.get("title", "")).strip()[:120]
        if not title:
            return None, "曲名を入力してください。"
        instrument = str(values.get("instrument", "")).strip()
        kind = str(values.get("kind", "")).strip()
        if instrument not in {"", "ウクレレ", "ギター"}:
            return None, "楽器が正しくありません。"
        if kind not in {"", "弾き語り", "ソロ弾き", "メロ弾き", "デュオ"}:
            return None, "形態が正しくありません。"
        video = str(values.get("video", "")).strip()[:500]
        video_id = ""
        if video:
            video_id = _youtube_video_id(video)
            if not video_id:
                return None, "YouTube動画のURLを入力してください。"
        normalized = _normalized_title(title)
        for item in materials:
            if item.get("id") == material_id:
                continue
            if _normalized_title(item.get("title", "")) == normalized:
                return None, f"同じ曲名がすでにあります（ID {item['id']}）。"
            existing_video = str(item.get("video", "")).strip()
            if video and (existing_video == video or (video_id and _youtube_video_id(existing_video) == video_id)):
                return None, f"同じ動画URLがすでにあります（ID {item['id']}）。"
        updated.update(
            title=title,
            instrument=instrument,
            kind=kind,
            artist=str(values.get("artist", "")).strip()[:120],
            video=video,
            note=str(values.get("note", "")).strip()[:500],
            genre=str(values.get("genre", "")).strip()[:80],
        )
    else:
        updated["active"] = action == "publish"

    write_url = os.environ.get("REPERTOIRE_SHEET_WRITE_URL", "").strip()
    if current.get("source") == "sheet":
        if not write_url:
            return None, "Google Sheet書き込み連携が設定されていないため変更できません。"
        payload = {
            "secret": os.environ.get("REPERTOIRE_SHEET_WRITE_SECRET", ""),
            "action": action,
            "id": material_id,
            **{key: updated.get(key, "") for key in ("title", "instrument", "kind", "artist", "video", "note", "genre")},
        }
        try:
            response = requests.post(write_url, json=payload, timeout=20)
            response.raise_for_status()
            result = response.json()
            if not result.get("ok"):
                raise RuntimeError(result.get("error") or "Google Sheetの更新に失敗しました。")
        except Exception as exc:
            return None, f"Google Sheetの更新に失敗しました: {str(exc)[:240]}"
    else:
        custom = load_json("carte:custom_materials", default=[])
        if not isinstance(custom, list):
            custom = []
        row = next((item for item in custom if int(item.get("id", 0) or 0) == material_id), None)
        if not row:
            return None, "曲が見つかりません。"
        row.update({key: updated.get(key) for key in ("title", "instrument", "kind", "artist", "video", "note", "genre", "active")})
        row["updated_at"] = _now()
        save_json("carte:custom_materials", custom)

    _material_cache.update(at=0.0, items=[])
    _record_material_history(material_id, action, current, updated)
    return updated, ""


def get_request(request_id: str) -> dict | None:
    return next((row for row in _requests() if str(row.get("id")) == str(request_id)), None)


def mark_request_added(request_id: str, material_id: int) -> None:
    rows = _requests()
    row = next((item for item in rows if str(item.get("id")) == str(request_id)), None)
    if not row:
        return
    row.update(status="added", material_id=material_id, added_at=_now())
    save_json("carte:requests", rows)


def next_lesson_groups() -> list[dict]:
    materials = {item["id"]: item for item in load_materials()}
    members = {item.get("user_id"): item for item in _carte_members()}
    grouped = {}
    for row in _progress():
        if not row.get("next_lesson"):
            continue
        material = materials.get(row.get("material_id"))
        if not material:
            continue
        user_id = row.get("user_id", "")
        if not user_id:
            continue
        group = grouped.setdefault(
            user_id,
            {
                "user_id": user_id,
                "display_name": row.get("display_name")
                or members.get(user_id, {}).get("display_name", "名前未登録"),
                "items": [],
            },
        )
        group["items"].append(
            {
                "id": material["id"],
                "title": material["title"],
                "artist": material.get("artist", ""),
                "video": material.get("video", ""),
                "teacher_note": row.get("teacher_note", ""),
                "student_note": row.get("student_note", ""),
            }
        )
    for group in grouped.values():
        group["items"].sort(key=lambda item: item["id"], reverse=True)
    return sorted(grouped.values(), key=lambda item: item["display_name"])


def build_next_lesson_message(group: dict) -> str:
    lines = [f"{group.get('display_name', '')}さん", "", "次回レッスンで取り組む曲です。"]
    for index, item in enumerate(group.get("items", []), start=1):
        title = item.get("title", "")
        artist = item.get("artist", "")
        lines.extend(["", f"{index}. {title}{' / ' + artist if artist else ''}"])
        videos = str(item.get("video", "")).split()
        if videos:
            lines.append(videos[0])
        note = item.get("teacher_note") or item.get("student_note")
        if note:
            lines.append(f"メモ: {str(note)[:300]}")
    lines.extend(["", "よろしくお願いします。"])
    return "\n".join(lines)[:4900]


def _progress():
    return load_json("carte:progress", default=[])


def _history():
    return load_json("carte:history", default=[])


def _student_rows(user_id):
    return [r for r in _progress() if r.get("user_id") == user_id]


def _prefs():
    """生徒ごとの設定。今は楽器だけ。{user_id: {"instrument": "ウクレレ"}}"""
    return load_json("carte:prefs", default={})


def _carte_members():
    """共通カルテ専用の生徒名簿。地域別の日程調整名簿とは混ぜない。"""
    members = load_json("carte:members", default=[])
    # 初回だけ、既存の関東カルテが使っていた kanto:members を引き継ぐ。
    from tenant_config import get_tenant
    if get_tenant().name == "kanto":
        known = {m.get("user_id") for m in members}
        changed = False
        for member in load_json("members", default=[]):
            if member.get("user_id") not in known:
                members.append(member)
                known.add(member.get("user_id"))
                changed = True
        if changed:
            save_json("carte:members", members)
    return members


def _upsert_carte_member(user_id, display_name):
    members = _carte_members()
    member = next((m for m in members if m.get("user_id") == user_id), None)
    if member:
        if member.get("display_name") != display_name:
            member["display_name"] = display_name
            save_json("carte:members", members)
        return
    members.append({"user_id": user_id, "display_name": display_name})
    save_json("carte:members", members)


def _is_done(row) -> bool:
    """画面側のisDoneと同じ判定をサーバーでも使う。"""
    if row.get("lesson_done") is True:
        return True
    return "lesson_done" not in row and row.get("status") == "completed"


def _requests():
    """まだシートに無い曲のリクエスト。"""
    return load_json("carte:requests", default=[])


def _public_requests(user_id):
    """生徒に見せる形。**リクエストした人の名前は含めない。**
    見送り（declined）にしたものは生徒側には出さない。"""
    out = []
    for row in _requests():
        if row.get("status") == "declined":
            continue
        votes = row.get("votes", [])
        out.append(
            {
                "id": row.get("id"),
                "title": row.get("title", ""),
                "artist": row.get("artist", ""),
                "instrument": row.get("instrument", ""),
                "comment": row.get("comment", ""),
                "status": row.get("status", "open"),
                "votes": len(votes),
                "voted": user_id in votes,
                "mine": row.get("user_id") == user_id,
            }
        )
    out.sort(key=lambda x: (-x["votes"], x["title"]))
    return out


def _popular_counts():
    """曲ごとの「実施済み」「やりたい」人数。生徒画面に個人名を出さずに人気度を見せるため。"""
    counts = {}
    for row in _progress():
        material_id = row.get("material_id")
        if material_id is None:
            continue
        bucket = counts.setdefault(material_id, {"done": 0, "wanted": 0})
        if _is_done(row):
            bucket["done"] += 1
        elif row.get("status") == "wanted":
            bucket["wanted"] += 1
    return counts


def _upsert_progress(user_id, display_name, material_id, changes, actor):
    materials = {m["id"]: m for m in load_materials()}
    if material_id not in materials:
        return None, "教材が見つかりません。"

    rows = _progress()
    row = next(
        (r for r in rows if r.get("user_id") == user_id and r.get("material_id") == material_id),
        None,
    )
    before = dict(row or {})
    if row is None:
        row = {
            "user_id": user_id,
            "display_name": display_name,
            "material_id": material_id,
            "status": "planned",
            "student_note": "",
            "teacher_note": "",
            "next_lesson": False,
            "assigned_by": actor,
            "created_at": _now(),
        }
        rows.append(row)

    allowed = {"status", "student_note", "lesson_done", "lesson_date"} if actor == "student" else {
        "status", "student_note", "teacher_note", "next_lesson", "lesson_done", "lesson_date"
    }
    for key in allowed:
        if key not in changes:
            continue
        value = changes[key]
        if key == "status" and value not in VALID_STATUSES:
            return None, "状態が正しくありません。"
        if key in {"student_note", "teacher_note"}:
            value = str(value or "").strip()[:1000]
        if key == "next_lesson":
            value = bool(value)
        if key == "lesson_done":
            value = bool(value)
        if key == "lesson_date":
            value = str(value or "").strip()
            if value:
                try:
                    datetime.strptime(value, "%Y-%m-%d")
                except ValueError:
                    return None, "授業日が正しくありません。"
        row[key] = value

    if row["status"] == "practicing" and not row.get("started_at"):
        row["started_at"] = _now()
    if row["status"] == "completed" and not row.get("completed_at"):
        row["completed_at"] = _now()
    elif row["status"] != "completed":
        row["completed_at"] = None
    row.update(display_name=display_name, updated_by=actor, updated_at=_now())
    save_json("carte:progress", rows)

    changed = {
        k: {"before": before.get(k), "after": row.get(k)}
        for k in ("status", "student_note", "teacher_note", "next_lesson", "lesson_done", "lesson_date")
        if before.get(k) != row.get(k)
    }
    if changed:
        history = _history()
        history.append(
            {
                "user_id": user_id,
                "display_name": display_name,
                "material_id": material_id,
                "actor": actor,
                "changed": changed,
                "timestamp": _now(),
            }
        )
        save_json("carte:history", history[-5000:])
    return row, None


STUDENT_HTML = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>マイカルテ</title>
<script src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script><style>.summary{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin:0 0 9px}.summary button{border:1px solid #dfe3e6;border-radius:8px;background:#f8faf9;padding:7px 3px;color:#465059;font-size:10px}.summary button b{display:block;color:#202428;font-size:16px;margin-bottom:1px}.summary button.on{border-color:#087f5b;background:#eaf7f0;color:#0f6e56}</style><style>
:root{--green:#087f5b;--amber:#a06a00;--line:#dfe3e6;--muted:#687078}*{box-sizing:border-box}body{margin:0;background:#f5f7f8;color:#202428;font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans",sans-serif}.head{position:sticky;top:0;z-index:5;background:#fff;padding:13px 12px;border-bottom:1px solid var(--line)}h1{font-size:19px;margin:0 0 10px}.tools{display:flex;gap:7px}.tools input{min-width:0;flex:1;height:41px;border:1px solid #bec5c9;border-radius:8px;padding:0 10px;font-size:15px}.tools select{width:110px;border:1px solid #bec5c9;border-radius:8px;background:#fff;padding:0 5px}.tools .req{flex:0 0 auto;height:41px;border:1px solid var(--green);border-radius:8px;background:#fff;color:var(--green);font-size:13px;font-weight:700;padding:0 12px}.reqrow{display:flex;align-items:center;gap:8px;padding:10px 0;border-bottom:1px solid #eef0f1}.reqrow:last-child{border-bottom:0}.reqname{flex:1;min-width:0}.reqname b{display:block;font-size:14px}.reqname span{display:block;color:var(--muted);font-size:10px;margin-top:2px}.metoo{flex:0 0 auto;height:34px;border:1px solid #bec5c9;border-radius:17px;background:#fff;font-size:12px;padding:0 13px}.metoo.on{background:var(--green);border-color:var(--green);color:#fff;font-weight:700}.added{flex:0 0 auto;font-size:10px;color:var(--green);font-weight:700}.count{font-size:11px;color:var(--muted);margin-top:7px}.sheet{margin:10px;background:#fff;border:1px solid var(--line);border-radius:9px;overflow:hidden}table{width:100%;border-collapse:collapse;table-layout:fixed}th{background:#f8faf9;font-size:12px;text-align:left;padding:9px;border-bottom:1px solid var(--line)}th:last-child{text-align:center;width:58%}td{border-bottom:1px solid var(--line);padding:10px 9px}.song{min-width:0}.song .t{line-height:1.35}.song b{font-size:14px}.song em{font-style:normal;color:var(--muted);font-size:11px;margin-left:6px}.song span.meta{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:4px}.tag{display:inline-block;font-size:9px;padding:2px 7px;border-radius:9px;margin-right:3px}.tag.uk{background:#e6f1fb;color:#0c447c}.tag.gt{background:#faece7;color:#712b13}.tag.kind{background:#f1efe8;color:#444441}.tag.genre{background:#eeedfe;color:#3c3489}.tag.pop{background:#fdf0e6;color:#993c1d}.vid{display:inline-block;font-size:9px;padding:2px 8px;border-radius:9px;margin-right:3px;background:#fdeaea;color:#a32d2d;font-weight:700;text-decoration:none}.nextmark{display:inline-block;font-size:10px;padding:2px 7px;border-radius:10px;background:#fff3d6;color:#854f0b;margin-top:3px}.groupsel{width:100%;height:44px;border:1px solid var(--green);border-radius:9px;background:#fff;color:#0f6e56;font-size:15px;font-weight:700;padding:0 10px;margin:0 0 10px}.cell{text-align:left;border-left:1px solid var(--line);cursor:pointer}.cell:active{background:#eaf7f0}.cell-content{display:flex;align-items:center;gap:8px;min-width:0}.status-block{flex:0 0 72px;text-align:center}.done{display:block;color:var(--green);font-weight:700;font-size:12px}.wanted{display:block;color:var(--amber);font-weight:700;font-size:12px}.notdone{display:block;color:#8a9298;font-size:12px}.lesson-date{display:block;color:#596168;font-size:10px;margin-top:3px}.memo-preview{flex:1;min-width:0;color:#5f4a12;background:#fff8dc;border-radius:5px;padding:5px 6px;font-size:10px;line-height:1.35;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.empty{text-align:center;color:var(--muted);padding:50px 10px}dialog{border:0;border-radius:12px;padding:0;width:calc(100% - 28px);max-width:390px;box-shadow:0 18px 60px rgba(0,0,0,.25)}dialog::backdrop{background:rgba(20,30,26,.42)}.modal{padding:22px}.modal h2{font-size:18px;margin:0 0 18px}.choice{display:flex;gap:6px;margin-bottom:16px}.choice label{flex:1;border:1px solid var(--line);border-radius:8px;padding:12px 3px;text-align:center;font-size:12px}.field{margin-top:14px}.field label{display:block;font-size:13px;font-weight:700;margin-bottom:6px}.field input,.field textarea{width:100%;border:1px solid #bec5c9;border-radius:7px;padding:10px;font-size:16px}.field input{height:44px}.field textarea{resize:vertical;min-height:88px}.actions{display:flex;gap:8px;margin-top:20px}.actions button{flex:1;height:43px;border-radius:7px;border:1px solid #bec5c9;background:#fff}.actions .save{background:var(--green);border-color:var(--green);color:#fff;font-weight:700}.video-dialog{max-width:760px;background:#111;color:#fff}.video-modal{padding:12px}.video-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}.video-head button{border:1px solid #555;border-radius:7px;background:#222;color:#fff;padding:7px 12px}.video-frame{position:relative;width:100%;padding-top:56.25%;background:#000}.video-frame iframe{position:absolute;inset:0;width:100%;height:100%;border:0}.video-fallback{display:block;color:#fff;text-align:center;font-size:12px;margin-top:12px}.notice{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);background:#183d31;color:#fff;padding:9px 16px;border-radius:7px;display:none;white-space:nowrap}
</style></head><body><div class="head"><h1 id="heading">マイカルテ</h1><select class="groupsel" id="groupSel"></select><div class="tools"><input id="q" placeholder="曲名・アーティストを検索"><select id="filter"><option value="all">すべて</option><option value="done">実施済み</option><option value="wanted">やりたい</option><option value="notdone">未実施</option><option value="next">次回レッスン</option><option value="popular">みんなのやりたい曲</option></select><button class="req" onclick="openRequests()">リクエスト</button></div><div class="count" id="count">読み込み中…</div></div><main class="sheet" id="sheet"><div class="empty">読み込み中…</div></main><dialog id="editor"><form class="modal" onsubmit="save(event)"><h2 id="editSong"></h2><div class="choice"><label><input type="radio" name="state" value="notdone"> 未実施</label><label><input type="radio" name="state" value="wanted"> ★ やりたい</label><label><input type="radio" name="state" value="done"> ✓ 実施済み</label></div><div class="field"><label for="lessonDate">授業日</label><input type="date" id="lessonDate"></div><div class="field"><label for="studentNote">自由メモ</label><textarea id="studentNote" maxlength="1000" placeholder="練習のポイントや気づいたことを自由に入力"></textarea></div><div class="actions"><button type="button" onclick="editor.close()">キャンセル</button><button class="save" id="saveButton">保存</button></div></form></dialog><dialog id="reqDialog"><div class="modal"><h2>リクエスト</h2><p style="color:#687078;font-size:12px;margin:-12px 0 16px">リストに無い曲をリクエストできます。名前は他の生徒には出ません。</p><form onsubmit="sendRequest(event)"><div class="field"><label for="reqTitle">曲名</label><input id="reqTitle" maxlength="120" placeholder="必須" required></div><div class="field"><label for="reqArtist">アーティスト</label><input id="reqArtist" maxlength="120" placeholder="わかれば"></div><div class="field"><label for="reqInstrument">楽器</label><select id="reqInstrument" style="width:100%;height:44px;border:1px solid #bec5c9;border-radius:7px;padding:0 10px;font-size:16px;background:#fff"><option value="">どちらでも</option><option value="ウクレレ">ウクレレ</option><option value="ギター">ギター</option></select></div><div class="field"><label for="reqComment">ひとこと</label><textarea id="reqComment" maxlength="300" placeholder="なぜやりたいか、どのバージョンかなど"></textarea></div><div class="actions"><button type="button" onclick="reqDialog.close()">閉じる</button><button class="save" id="reqButton">送信</button></div></form><div style="margin-top:22px"><h2 style="font-size:15px;margin:0 0 4px">みんなのリクエスト</h2><p style="color:#687078;font-size:11px;margin:0 0 8px">同じ曲をやりたければ「私も」を押してください</p><div id="reqList"></div></div></div></dialog><dialog id="videoDialog" class="video-dialog"><div class="video-modal"><div class="video-head"><strong>YouTube</strong><button type="button" onclick="closeVideo()">閉じる</button></div><div class="video-frame"><iframe id="videoFrame" title="YouTube動画" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share" allowfullscreen></iframe></div><a id="videoFallback" class="video-fallback" target="_blank" rel="noopener">再生できない場合はYouTubeで開く</a></div></dialog><div class="notice" id="notice">保存しました</div><script>
const LIFF_ID='__LIFF_ID__';let token='',materials=[],progress={},popular={},requests=[],editingId=null,group='';
const KIND_ORDER=['弾き語り','ソロ弾き','メロ弾き','デュオ'];
const INS_ORDER=['ウクレレ','ギター'];
function inGroup(m,v){if(!v)return true;if(v==='unset')return !m.kind;if(v.startsWith('ins:'))return m.instrument===v.slice(4);if(v.startsWith('pair:')){let[i,k]=v.slice(5).split('|');return m.instrument===i&&m.kind===k}return true}
function buildGroups(){
 let byIns={},unset=0;
 for(let m of materials){if(!m.kind)unset++;if(!m.instrument)continue;(byIns[m.instrument]=byIns[m.instrument]||{})[m.kind||'']=((byIns[m.instrument]||{})[m.kind||'']||0)+1}
 let names=Object.keys(byIns).sort((a,b)=>{let x=INS_ORDER.indexOf(a),y=INS_ORDER.indexOf(b);return (x<0?99:x)-(y<0?99:y)||a.localeCompare(b,'ja')});
 let html=`<option value="">すべて（${materials.length}）</option>`;
 for(let name of names){
   let kinds=Object.keys(byIns[name]).filter(k=>k);
   kinds.sort((a,b)=>{let x=KIND_ORDER.indexOf(a),y=KIND_ORDER.indexOf(b);return (x<0?99:x)-(y<0?99:y)||a.localeCompare(b,'ja')});
   let total=Object.values(byIns[name]).reduce((s,n)=>s+n,0);
   html+=`<optgroup label="${esc(name)}"><option value="ins:${esc(name)}">${esc(name)} すべて（${total}）</option>`;
   for(let k of kinds)html+=`<option value="pair:${esc(name)}|${esc(k)}">${esc(name)} ${esc(k)}（${byIns[name][k]}）</option>`;
   html+=`</optgroup>`;
 }
 if(unset)html+=`<option value="unset">未分類（${unset}）</option>`;
 groupSel.innerHTML=html;
}
function setGroup(v,redraw){group=v;groupSel.value=v;if(groupSel.value!==v){group='';groupSel.value=''}try{localStorage.setItem('carteGroup',group)}catch(e){}if(redraw)draw()}const esc=s=>String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const isDone=p=>p?.lesson_done===true||(!('lesson_done' in (p||{}))&&p?.status==='completed');const stateOf=p=>isDone(p)?'done':(p?.status==='wanted'?'wanted':'notdone');const stateLabel=s=>s==='done'?'✓ 実施済み':(s==='wanted'?'★ やりたい':'未実施');const lessonDate=p=>p?.lesson_date||(p?.status==='completed'&&p?.completed_at?p.completed_at.slice(0,10):'');const jaDate=s=>s?s.replace(/^(\d{4})-(\d{2})-(\d{2})$/,'$1/$2/$3'):'';
// LIFFはIDトークンをブラウザに保存して使い回す。期限（約1時間）が切れたものを
// そのまま送ると、以後ずっと「認証に失敗しました」になる。切れていたらログインし直す。
function liffTokenExp(t){try{return JSON.parse(atob(t.split('.')[1].replace(/-/g,'+').replace(/_/g,'/'))).exp*1000}catch(e){return 0}}
function liffTokenFresh(t){return !!t&&liffTokenExp(t)-60000>Date.now()}
// 入り直せたら true。短時間に繰り返すと無限ループになるので30秒に1回まで。
// falseのときは呼び出し側でそのまま進め、サーバーからのエラー文を画面に出す。
function liffRelogin(){let last=0;try{last=Number(sessionStorage.getItem('liffRelogin')||0)}catch(e){}if(Date.now()-last<30000)return false;try{sessionStorage.setItem('liffRelogin',String(Date.now()))}catch(e){}
 if(liff.isInClient()){location.reload();return true}  // LINEアプリ内はlogoutが効かない。開き直せば新しいトークンが来る
 try{liff.logout()}catch(e){}liff.login({redirectUri:location.href});return true}
async function api(url,body){let r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...body,idToken:token})});if(r.status===401&&liffRelogin())throw Error('ログインの有効期限が切れました。読み込み直しています…');let d=await r.json();if(!r.ok)throw Error(d.error||'エラー');return d}
function tags(m){let out='';if(m.instrument)out+=`<span class="tag ${m.instrument==='ウクレレ'?'uk':'gt'}">${esc(m.instrument)}</span>`;if(m.kind)out+=`<span class="tag kind">${esc(m.kind)}</span>`;if(m.genre)out+=`<span class="tag genre">${esc(m.genre)}</span>`;let w=popular[m.id]?.wanted||0;if(w)out+=`<span class="tag pop">★${w}人</span>`;return out}
const videoUrls=s=>String(s||'').match(/https?:\/\/[^\s]+/g)||[];
function youtubeId(url){try{let u=new URL(url),h=u.hostname.toLowerCase().replace(/^www\./,'');if(!['youtube.com','m.youtube.com','music.youtube.com','youtube-nocookie.com','youtu.be'].includes(h))return '';let id=h==='youtu.be'?u.pathname.split('/')[1]:u.searchParams.get('v');if(!id){let p=u.pathname.split('/').filter(Boolean);if(['embed','shorts','live'].includes(p[0]))id=p[1]}return /^[A-Za-z0-9_-]{6,15}$/.test(id||'')?id:''}catch(e){return ''}}
function videos(m){let us=videoUrls(m.video);return us.map((u,i)=>`<a class="vid" href="${esc(u)}" target="_blank" rel="noopener" data-video="${esc(u)}" onclick="playVideo(event,this.dataset.video)">▶ ${us.length>1?'動画'+(i+1):'動画'}</a>`).join('')}
function playVideo(e,url){e.preventDefault();e.stopPropagation();let id=youtubeId(url);if(!id){if(window.liff&&liff.openWindow){liff.openWindow({url,external:true})}else{window.open(url,'_blank','noopener')}return}videoFrame.src='https://www.youtube-nocookie.com/embed/'+encodeURIComponent(id)+'?playsinline=1&rel=0';videoFallback.href=url;if(!videoDialog.open)videoDialog.showModal()}
function closeVideo(){videoFrame.removeAttribute('src');if(videoDialog.open)videoDialog.close()}
function chooseSummary(value){filter.value=value;draw()}
function renderSummary(){if(!document.getElementById('summary')){let el=document.createElement('div');el.id='summary';el.className='summary';groupSel.insertAdjacentElement('afterend',el)}let done=0,wanted=0,next=0;for(let p of Object.values(progress)){if(isDone(p))done++;else if(p?.status==='wanted')wanted++;if(p?.next_lesson)next++}summary.innerHTML=[['done','実施済み',done],['wanted','やりたい',wanted],['next','次回',next]].map(x=>`<button type="button" class="${filter.value===x[0]?'on':''}" onclick="chooseSummary('${x[0]}')"><b>${x[2]}</b>${x[1]}</button>`).join('')}
function draw(){let query=q.value.toLowerCase(),f=filter.value;let xs=materials.filter(m=>{let st=stateOf(progress[m.id]);if(!inGroup(m,group))return false;if(!(m.title+' '+m.artist+' '+m.kind+' '+m.instrument+' '+(m.genre||'')).toLowerCase().includes(query))return false;if(f==='all')return true;if(f==='next')return !!progress[m.id]?.next_lesson;if(f==='popular')return (popular[m.id]?.wanted||0)>0;return f===st});
 if(f==='popular')xs=xs.slice().sort((a,b)=>(popular[b.id]?.wanted||0)-(popular[a.id]?.wanted||0));
 renderSummary();count.textContent=xs.length+'曲';sheet.innerHTML=xs.length?`<table><thead><tr><th>曲名</th><th>授業・メモ</th></tr></thead><tbody>${xs.map(m=>{let p=progress[m.id],st=stateOf(p),d=lessonDate(p);return `<tr><td class="song"><div class="t"><b>${esc(m.title)}</b>${m.artist?`<em>${esc(m.artist)}</em>`:''}</div><span class="meta">${tags(m)}${videos(m)}</span></td><td class="cell" onclick="openEditor(${m.id})"><div class="cell-content"><div class="status-block"><span class="${st}">${stateLabel(st)}</span>${d?`<span class="lesson-date">${jaDate(d)}</span>`:''}${p?.next_lesson?'<span class="nextmark">▶ 次回</span>':''}</div>${p?.student_note?`<span class="memo-preview" title="${esc(p.student_note)}">${esc(p.student_note)}</span>`:''}</div></td></tr>`}).join('')}</tbody></table>`:'<div class="empty">該当する曲はありません</div>'}
function drawRequests(){reqList.innerHTML=requests.length?requests.map(r=>`<div class="reqrow"><span class="reqname"><b>${esc(r.title)}</b><span>${esc([r.artist,r.instrument].filter(Boolean).join(' ／ '))||'&nbsp;'}</span></span>${r.status==='added'?'<span class="added">リストに追加済み</span>':`<button class="metoo${r.voted?' on':''}" onclick="vote('${esc(r.id)}')">${r.voted?'私も ✓':'私も'} ${r.votes}</button>`}</div>`).join(''):'<p class="empty" style="padding:20px 0">まだリクエストはありません</p>'}
function openRequests(){drawRequests();reqDialog.showModal()}
async function vote(id){try{let d=await api('/api/carte/request/vote',{id});requests=d.requests;drawRequests()}catch(e){alert(e.message)}}
async function sendRequest(e){e.preventDefault();let b=reqButton;b.disabled=true;b.textContent='送信中';try{let d=await api('/api/carte/request',{title:reqTitle.value,artist:reqArtist.value,instrument:reqInstrument.value,comment:reqComment.value});requests=d.requests;reqTitle.value='';reqArtist.value='';reqComment.value='';drawRequests();notice.textContent='リクエストを送りました';notice.style.display='block';setTimeout(()=>notice.style.display='none',2000)}catch(err){alert(err.message)}finally{b.disabled=false;b.textContent='送信'}}
function openEditor(id){let m=materials.find(x=>x.id===id),p=progress[id];editingId=id;editSong.textContent=m.title;document.querySelector(`input[name="state"][value="${stateOf(p)}"]`).checked=true;lessonDate.value=lessonDate(p);studentNote.value=p?.student_note||'';editor.showModal()}
async function save(e){e.preventDefault();let st=document.querySelector('input[name="state"]:checked')?.value||'notdone',done=st==='done',button=saveButton;button.disabled=true;button.textContent='保存中';try{let d=await api('/api/carte/progress',{material_id:editingId,lesson_done:done,lesson_date:lessonDate.value,student_note:studentNote.value,status:done?'completed':(st==='wanted'?'wanted':'planned')});progress[editingId]=d.progress;editor.close();draw();notice.style.display='block';setTimeout(()=>notice.style.display='none',1800)}catch(err){alert(err.message)}finally{button.disabled=false;button.textContent='保存'}}
async function main(){await liff.init({liffId:LIFF_ID});if(!liff.isLoggedIn()){liff.login({redirectUri:location.href});return}token=liff.getIDToken();if(!liffTokenFresh(token)&&liffRelogin())return;let d=await api('/api/carte/me',{});materials=d.materials;popular=d.popular||{};requests=d.requests||[];progress=Object.fromEntries(d.progress.map(p=>[p.material_id,p]));heading.textContent=(d.display_name||'')+'さんのカルテ';
 buildGroups();
 let saved=null;try{saved=localStorage.getItem('carteGroup')}catch(e){}
 setGroup(saved!==null?saved:(d.instrument?'ins:'+d.instrument:''),false);
 groupSel.onchange=()=>setGroup(groupSel.value,true);
 draw();q.oninput=draw;filter.onchange=draw;editor.addEventListener('click',e=>{if(e.target===editor)editor.close()});reqDialog.addEventListener('click',e=>{if(e.target===reqDialog)reqDialog.close()});videoDialog.addEventListener('click',e=>{if(e.target===videoDialog)closeVideo()});videoDialog.addEventListener('close',()=>videoFrame.removeAttribute('src'))}main().catch(e=>sheet.innerHTML='<div class="empty">'+esc(e.message)+'</div>');
</script></body></html>"""


ADMIN_HTML = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>生徒カルテ管理</title><style>
:root{--green:#087f5b;--amber:#a06a00;--line:#dfe3e6;--muted:#687078;--bg:#f5f7f8}*{box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans",sans-serif;margin:0;background:var(--bg);color:#202428}header{height:62px;background:#fff;border-bottom:1px solid var(--line);display:flex;align-items:center;padding:0 24px}header h1{font-size:20px;margin:0}.page{padding:20px 24px}.toolbar{display:flex;align-items:center;gap:10px;margin-bottom:12px}.toolbar input,.toolbar select{height:40px;border:1px solid #bec5c9;border-radius:7px;background:#fff;padding:0 12px;font-size:14px}.toolbar input{width:360px}.toolbar button{height:40px;border:1px solid #bec5c9;border-radius:7px;background:#fff;padding:0 14px;font-size:13px;cursor:pointer}.toolbar button:hover{background:#f3f6f5}.toolbar button:disabled{color:#9aa1a6;cursor:default}.count{color:var(--muted);font-size:13px;white-space:nowrap}.link{color:#0f6e56;font-size:13px;text-decoration:none;border-bottom:1px solid #0f6e56;white-space:nowrap}.hint{margin-left:auto;color:var(--muted);font-size:12px}@media(max-width:1100px){.hint{display:none}}.sheet{background:#fff;border:1px solid var(--line);border-radius:9px;overflow:auto;height:calc(100vh - 135px)}table{border-collapse:separate;border-spacing:0;min-width:100%;white-space:nowrap}th,td{border-right:1px solid var(--line);border-bottom:1px solid var(--line)}th{height:50px;padding:8px 12px;background:#f8faf9;font-size:13px;text-align:center;position:sticky;top:0;z-index:2}.song-head{left:0;z-index:4;text-align:left;min-width:290px}.song{position:sticky;left:0;z-index:1;background:#fff;min-width:290px;max-width:290px;padding:9px 12px}.song .t{overflow:hidden;text-overflow:ellipsis}.song em{font-style:normal;color:var(--muted);font-size:12px;margin-left:7px}.song>span{display:block;overflow:hidden;text-overflow:ellipsis;margin-top:4px}.tag{display:inline-block;font-size:10px;padding:2px 8px;border-radius:10px;margin-right:4px;white-space:nowrap}.tag.uk{background:#e6f1fb;color:#0c447c}.tag.gt{background:#faece7;color:#712b13}.tag.kind{background:#f1efe8;color:#444441}.tag.genre{background:#eeedfe;color:#3c3489}.vid{display:inline-block;font-size:10px;padding:2px 8px;border-radius:10px;margin-right:4px;background:#fdeaea;color:#a32d2d;font-weight:700;text-decoration:none}.vid:hover{background:#f7d4d4}.student{min-width:240px;max-width:240px}.cell{min-width:240px;max-width:240px;height:58px;padding:5px 8px;cursor:pointer;background:#fff}.cell:hover{background:#eef8f3}.cell-content{display:flex;align-items:center;gap:9px;min-width:0}.status-block{flex:0 0 86px;text-align:center}.done{display:block;color:#087f5b;font-weight:700;font-size:13px}.wanted{display:block;color:var(--amber);font-weight:700;font-size:13px}.notdone{display:block;color:#8a9298;font-size:13px}.lesson-date{display:block;color:#596168;font-size:11px;margin-top:3px}.memo-preview{flex:1;min-width:0;color:#5f4a12;background:#fff8dc;border-radius:5px;padding:5px 7px;font-size:11px;line-height:1.35;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.empty{padding:50px;text-align:center;color:var(--muted)}dialog{border:0;border-radius:12px;padding:0;box-shadow:0 18px 60px rgba(0,0,0,.25);width:min(420px,calc(100% - 30px))}dialog::backdrop{background:rgba(20,30,26,.4)}.modal{padding:24px}.modal h2{font-size:19px;margin:0 0 5px}.modal .who{color:var(--muted);font-size:13px;margin-bottom:20px}.choice{display:flex;gap:6px;margin-bottom:16px}.choice label{flex:1;border:1px solid var(--line);border-radius:8px;padding:12px 4px;text-align:center;cursor:pointer;font-size:13px}.nextbox{display:block;border:1px solid #e0d3b0;background:#fffbf0;border-radius:8px;padding:11px 12px;font-size:13px;cursor:pointer}.student-set{display:block;margin-top:5px}.student-set select{height:26px;border:1px solid #cfd5d8;border-radius:5px;background:#fff;font-size:11px;padding:0 4px}.nextmark{display:inline-block;font-size:10px;padding:2px 7px;border-radius:10px;background:#fff3d6;color:#854f0b;margin-top:3px}.field{margin-top:14px}.field label{display:block;font-size:13px;font-weight:700;margin-bottom:6px}.field input,.field textarea{width:100%;border:1px solid #bec5c9;border-radius:7px;padding:0 10px;font-size:16px}.field input{height:43px}.field textarea{height:96px;padding:10px;resize:vertical;font-family:inherit}.actions{display:flex;gap:8px;margin-top:20px}.actions button{flex:1;height:42px;border-radius:7px;border:1px solid #bec5c9;background:#fff;cursor:pointer}.actions .save{background:var(--green);border-color:var(--green);color:#fff;font-weight:700}.video-dialog{width:min(760px,calc(100% - 30px));background:#111;color:#fff}.video-modal{padding:14px}.video-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}.video-head button{border:1px solid #555;border-radius:7px;background:#222;color:#fff;padding:7px 12px;cursor:pointer}.video-frame{position:relative;width:100%;padding-top:56.25%;background:#000}.video-frame iframe{position:absolute;inset:0;width:100%;height:100%;border:0}.video-fallback{display:block;color:#fff;text-align:center;font-size:12px;margin-top:12px}.notice{position:fixed;right:24px;bottom:20px;background:#183d31;color:#fff;padding:10px 16px;border-radius:7px;display:none}@media(max-width:700px){.page{padding:12px}.toolbar{flex-wrap:wrap}.toolbar input{width:100%}.hint{margin-left:0}.song-head,.song{min-width:220px;max-width:220px}}
</style></head><body><header><h1>生徒カルテ管理</h1></header><main class="page"><div class="toolbar"><input id="q" placeholder="曲名・アーティストを検索"><select id="filter"><option value="all">すべての曲</option><option value="used">実施記録がある曲</option><option value="wanted">やりたい人がいる曲</option><option value="next">次回レッスン曲</option></select><span class="count" id="count">読み込み中…</span><button id="syncBtn" onclick="sync()">シートを再読み込み</button><a class="link" href="/admin/carte/next">次回まとめ</a><a class="link" href="/admin/carte/ranking">ランキング</a><a class="link" href="/admin/carte/requests">リクエスト曲</a><a class="link" href="/admin/carte/history">更新履歴</a><span class="hint">セルをクリックして実施状況・授業日・メモを入力</span></div><div class="sheet" id="sheet"><div class="empty">読み込み中…</div></div></main><dialog id="editor"><form class="modal" onsubmit="save(event)"><h2 id="editSong"></h2><div class="who" id="editStudent"></div><div class="choice"><label><input type="radio" name="state" value="notdone"> 未実施</label><label><input type="radio" name="state" value="wanted"> ★ やりたい</label><label><input type="radio" name="state" value="done"> ✓ 実施済み</label></div><label class="nextbox"><input type="checkbox" id="nextLesson"> ▶ 次回レッスンでやる</label><div class="field"><label for="lessonDate">授業日</label><input type="date" id="lessonDate"></div><div class="field"><label for="studentNote">生徒メモ</label><textarea id="studentNote" maxlength="1000" placeholder="生徒が書いたメモを確認・編集できます"></textarea></div><div class="actions"><button type="button" onclick="editor.close()">キャンセル</button><button class="save" id="saveButton">保存</button></div></form></dialog><dialog id="videoDialog" class="video-dialog"><div class="video-modal"><div class="video-head"><strong>YouTube</strong><button type="button" onclick="closeVideo()">閉じる</button></div><div class="video-frame"><iframe id="videoFrame" title="YouTube動画" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share" allowfullscreen></iframe></div><a id="videoFallback" class="video-fallback" target="_blank" rel="noopener">再生できない場合はYouTubeで開く</a></div></dialog><div class="notice" id="notice">保存しました</div><script>
let data,progress={},editing={};const esc=s=>String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const key=(uid,mid)=>uid+'|'+mid;const isDone=p=>p?.lesson_done===true||(!('lesson_done' in (p||{}))&&p?.status==='completed');const stateOf=p=>isDone(p)?'done':(p?.status==='wanted'?'wanted':'notdone');const stateLabel=s=>s==='done'?'✓ 実施済み':(s==='wanted'?'★ やりたい':'未実施');const lessonDate=p=>p?.lesson_date||(p?.status==='completed'&&p?.completed_at?p.completed_at.slice(0,10):'');const jaDate=s=>s?s.replace(/^(\d{4})-(\d{2})-(\d{2})$/,'$1/$2/$3'):'';
async function load(){let r=await fetch('/admin/carte/data'),d=await r.json();if(!r.ok)throw Error(d.error||'取得失敗');data=d;progress=Object.fromEntries(d.progress.map(p=>[key(p.user_id,p.material_id),p]));draw()}
async function sync(){let b=syncBtn;b.disabled=true;b.textContent='読み込み中…';try{let r=await fetch('/admin/carte/sync',{method:'POST'}),d=await r.json();if(!r.ok)throw Error(d.error||'シートの読み込みに失敗しました');await load();notice.textContent=d.count+'曲を読み込みました';notice.style.display='block';setTimeout(()=>notice.style.display='none',2200)}catch(e){alert(e.message)}finally{b.disabled=false;b.textContent='シートを再読み込み'}}
function tags(m){let out='';if(m.instrument)out+=`<span class="tag ${m.instrument==='ウクレレ'?'uk':'gt'}">${esc(m.instrument)}</span>`;if(m.kind)out+=`<span class="tag kind">${esc(m.kind)}</span>`;if(m.genre)out+=`<span class="tag genre">${esc(m.genre)}</span>`;return out||'<span class="tag kind">形態未設定</span>'}
const videoUrls=s=>String(s||'').match(/https?:\/\/[^\s]+/g)||[];
function youtubeId(url){try{let u=new URL(url),h=u.hostname.toLowerCase().replace(/^www\./,'');if(!['youtube.com','m.youtube.com','music.youtube.com','youtube-nocookie.com','youtu.be'].includes(h))return '';let id=h==='youtu.be'?u.pathname.split('/')[1]:u.searchParams.get('v');if(!id){let p=u.pathname.split('/').filter(Boolean);if(['embed','shorts','live'].includes(p[0]))id=p[1]}return /^[A-Za-z0-9_-]{6,15}$/.test(id||'')?id:''}catch(e){return ''}}
function videos(m){let us=videoUrls(m.video);return us.map((u,i)=>`<a class="vid" href="${esc(u)}" target="_blank" rel="noopener" data-video="${esc(u)}" onclick="playVideo(event,this.dataset.video)">▶ ${us.length>1?'動画'+(i+1):'動画'}</a>`).join('')}
function playVideo(e,url){e.preventDefault();e.stopPropagation();let id=youtubeId(url);if(!id){window.open(url,'_blank','noopener');return}videoFrame.src='https://www.youtube-nocookie.com/embed/'+encodeURIComponent(id)+'?playsinline=1&rel=0';videoFallback.href=url;if(!videoDialog.open)videoDialog.showModal()}
function closeVideo(){videoFrame.removeAttribute('src');if(videoDialog.open)videoDialog.close()}
function draw(){let query=q.value.toLowerCase(),f=filter.value;let materials=data.materials.filter(m=>{let matches=(m.title+' '+m.artist+' '+m.kind+' '+m.instrument+' '+(m.genre||'')).toLowerCase().includes(query);let keep=f==='all'||(f==='used'&&data.students.some(s=>progress[key(s.user_id,m.id)]))||(f==='wanted'&&data.students.some(s=>stateOf(progress[key(s.user_id,m.id)])==='wanted'))||(f==='next'&&data.students.some(s=>progress[key(s.user_id,m.id)]?.next_lesson));return matches&&keep});count.textContent=materials.length+'曲 × '+data.students.length+'人';sheet.innerHTML=data.students.length?`<table><thead><tr><th class="song-head">曲名</th>${data.students.map(s=>`<th class="student">${esc(s.display_name||'名前未登録')}<label class="student-set"><select onchange="setInstrument('${esc(s.user_id)}',this.value)"><option value="">楽器: すべて</option><option value="ウクレレ"${s.instrument==='ウクレレ'?' selected':''}>ウクレレのみ</option><option value="ギター"${s.instrument==='ギター'?' selected':''}>ギターのみ</option></select></label></th>`).join('')}</tr></thead><tbody>${materials.map(m=>`<tr><td class="song"><div class="t"><b>${esc(m.title)}</b>${m.artist?`<em>${esc(m.artist)}</em>`:''}</div><span>${tags(m)}${videos(m)}</span></td>${data.students.map(s=>cell(m,s)).join('')}</tr>`).join('')}</tbody></table>`:'<div class="empty">生徒がまだ登録されていません</div>'}
function cell(m,s){let p=progress[key(s.user_id,m.id)],st=stateOf(p),d=lessonDate(p);return `<td class="cell" onclick="openEditor('${esc(s.user_id)}',${m.id})"><div class="cell-content"><div class="status-block"><span class="${st}">${stateLabel(st)}</span>${d?`<span class="lesson-date">${jaDate(d)}</span>`:''}${p?.next_lesson?'<span class="nextmark">▶ 次回</span>':''}</div>${p?.student_note?`<span class="memo-preview" title="${esc(p.student_note)}">${esc(p.student_note)}</span>`:''}</div></td>`}
async function setInstrument(uid,instrument){try{let r=await fetch('/admin/carte/student',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_id:uid,instrument})}),d=await r.json();if(!r.ok)throw Error(d.error||'保存に失敗しました');let s=data.students.find(x=>x.user_id===uid);if(s)s.instrument=instrument;notice.textContent=instrument?instrument+'の曲だけを表示する設定にしました':'すべての楽器を表示する設定にしました';notice.style.display='block';setTimeout(()=>notice.style.display='none',2200)}catch(e){alert(e.message);load()}}
function openEditor(uid,mid){let s=data.students.find(x=>x.user_id===uid),m=data.materials.find(x=>x.id===mid),p=progress[key(uid,mid)];editing={uid,mid};editSong.textContent=m.title;editStudent.textContent=(s.display_name||'名前未登録')+'さん';document.querySelector(`input[name="state"][value="${stateOf(p)}"]`).checked=true;nextLesson.checked=!!p?.next_lesson;lessonDate.value=lessonDate(p);studentNote.value=p?.student_note||'';editor.showModal()}
async function save(e){e.preventDefault();let st=document.querySelector('input[name="state"]:checked')?.value||'notdone',done=st==='done',button=saveButton;button.disabled=true;button.textContent='保存中';let body={user_id:editing.uid,material_id:editing.mid,lesson_done:done,lesson_date:lessonDate.value,student_note:studentNote.value,next_lesson:nextLesson.checked,status:done?'completed':(st==='wanted'?'wanted':'planned')};try{let r=await fetch('/admin/carte/progress',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),d=await r.json();if(!r.ok)throw Error(d.error||'保存失敗');progress[key(editing.uid,editing.mid)]=d.progress;editor.close();draw();notice.textContent='保存しました';notice.style.display='block';setTimeout(()=>notice.style.display='none',1800)}catch(err){alert(err.message)}finally{button.disabled=false;button.textContent='保存'}}
q.oninput=draw;filter.onchange=draw;editor.addEventListener('click',e=>{if(e.target===editor)editor.close()});videoDialog.addEventListener('click',e=>{if(e.target===videoDialog)closeVideo()});videoDialog.addEventListener('close',()=>videoFrame.removeAttribute('src'));load().catch(e=>sheet.innerHTML='<div class="empty">'+esc(e.message)+'</div>');
</script></body></html>"""


RANKING_HTML = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>曲ランキング</title><style>
:root{--green:#087f5b;--amber:#a06a00;--line:#dfe3e6;--muted:#687078;--bg:#f5f7f8}*{box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans",sans-serif;margin:0;background:var(--bg);color:#202428}header{height:62px;background:#fff;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:16px;padding:0 24px}header h1{font-size:20px;margin:0}header a{margin-left:auto;color:#0f6e56;font-size:13px;text-decoration:none;border-bottom:1px solid #0f6e56}.page{padding:20px 24px}.count{color:var(--muted);font-size:13px;margin:0 0 14px}.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}.card{background:#fff;border:1px solid var(--line);border-radius:9px;padding:16px 18px}.card h2{font-size:15px;margin:0 0 4px}.card p.note{color:var(--muted);font-size:12px;margin:0 0 12px}.filters{display:flex;gap:7px;margin:0 0 14px}.filters button{height:34px;border:1px solid #bec5c9;border-radius:7px;background:#fff;padding:0 14px;font-size:13px;cursor:pointer}.filters button.on{background:#0f6e56;border-color:#0f6e56;color:#fff;font-weight:700}.breakdown{border-top:1px solid #f0f2f3;border-bottom:1px solid #f0f2f3;padding:9px 0;margin:0 0 8px;font-size:11px;color:var(--muted);line-height:1.9}.breakdown span{margin-right:10px}.breakdown b{color:#202428}.row{display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid #f0f2f3}.row:last-child{border-bottom:0}.rank{flex:0 0 22px;text-align:right;color:var(--muted);font-size:12px}.name{flex:1;min-width:0}.name b{font-size:14px}.name em{font-style:normal;color:var(--muted);font-size:11px;margin-left:6px}.name .sub{display:block;margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.tag{display:inline-block;font-size:10px;padding:2px 8px;border-radius:10px;margin-right:4px;white-space:nowrap}.tag.uk{background:#e6f1fb;color:#0c447c}.tag.gt{background:#faece7;color:#712b13}.tag.kind{background:#f1efe8;color:#444441}.bar{flex:0 0 84px;height:6px;background:#eef0f1;border-radius:3px;overflow:hidden}.bar i{display:block;height:100%}.done .bar i{background:var(--green)}.wanted .bar i{background:var(--amber)}.num{flex:0 0 44px;text-align:right;font-size:13px;font-weight:700}.done .num{color:var(--green)}.wanted .num{color:var(--amber)}.empty{color:var(--muted);font-size:13px;padding:16px 0;margin:0}
</style></head><body><header><h1>曲ランキング</h1><a href="/admin/carte">カルテに戻る</a></header><main class="page"><p class="count" id="count">読み込み中…</p><div class="filters" id="filters"><button class="on" data-ins="all">すべて</button><button data-ins="ウクレレ">ウクレレ</button><button data-ins="ギター">ギター</button></div><div class="cols"><section class="card done"><h2>✓ 実施済みが多い曲</h2><p class="note">レッスンで実際に扱った人数の多い順</p><div class="breakdown" id="doneSum"></div><div id="doneList"></div></section><section class="card wanted"><h2>★ やりたいが多い曲</h2><p class="note">希望が集まっている順。次に用意する曲の参考に</p><div class="breakdown" id="wantedSum"></div><div id="wantedList"></div></section></div></main><script>
const esc=s=>String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const isDone=p=>p?.lesson_done===true||(!('lesson_done' in (p||{}))&&p?.status==='completed');
let data=null,ins='all';
const insClass=s=>s==='ウクレレ'?'uk':(s==='ギター'?'gt':'kind');
function tags(m){let out='';if(m.instrument)out+=`<span class="tag ${insClass(m.instrument)}">${esc(m.instrument)}</span>`;out+=`<span class="tag kind">${esc(m.kind||'形態未設定')}</span>`;return out}
function summary(el,xs){if(!xs.length){el.innerHTML='';return}
 let byIns={},byKind={};for(let x of xs){let i=x.m.instrument||'楽器未設定',k=x.m.kind||'形態未設定';byIns[i]=(byIns[i]||0)+1;byKind[k]=(byKind[k]||0)+1}
 const line=o=>Object.entries(o).sort((a,b)=>b[1]-a[1]).map(([k,v])=>`<span>${esc(k)} <b>${v}曲</b></span>`).join('');
 el.innerHTML=`<div>${line(byIns)}</div><div>${line(byKind)}</div>`}
function render(listEl,sumEl,map,titles){let xs=Object.entries(map).map(([id,n])=>({m:titles[id],n})).filter(x=>x.m).sort((a,b)=>b.n-a.n||a.m.title.localeCompare(b.m.title,'ja'));summary(sumEl,xs);if(!xs.length){listEl.innerHTML='<p class="empty">まだありません</p>';return}let max=xs[0].n;listEl.innerHTML=xs.map((x,i)=>`<div class="row"><span class="rank">${i+1}</span><span class="name"><b>${esc(x.m.title)}</b>${x.m.artist?`<em>${esc(x.m.artist)}</em>`:''}<span class="sub">${tags(x.m)}</span></span><span class="bar"><i style="width:${Math.round(x.n/max*100)}%"></i></span><span class="num">${x.n}人</span></div>`).join('')}
function draw(){let titles=Object.fromEntries(data.materials.map(m=>[m.id,m])),done={},wanted={},shown=0;
 for(let m of data.materials){if(ins==='all'||m.instrument===ins)shown++}
 for(let p of data.progress){let m=titles[p.material_id];if(!m)continue;if(ins!=='all'&&m.instrument!==ins)continue;if(isDone(p))done[p.material_id]=(done[p.material_id]||0)+1;else if(p.status==='wanted')wanted[p.material_id]=(wanted[p.material_id]||0)+1}
 count.textContent='生徒 '+data.students.length+'人 ／ 曲 '+shown+'曲'+(ins==='all'?'':'（'+ins+'のみ）');
 render(doneList,doneSum,done,titles);render(wantedList,wantedSum,wanted,titles)}
async function load(){let r=await fetch('/admin/carte/data'),d=await r.json();if(!r.ok)throw Error(d.error||'取得に失敗しました');data=d;draw()}
filters.querySelectorAll('button').forEach(b=>b.onclick=()=>{ins=b.dataset.ins;filters.querySelectorAll('button').forEach(x=>x.classList.toggle('on',x===b));draw()});
load().catch(e=>{doneList.innerHTML='<p class="empty">'+esc(e.message)+'</p>';wantedList.innerHTML=''});
</script></body></html>"""


HISTORY_HTML = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>更新履歴</title><style>
:root{--green:#087f5b;--amber:#a06a00;--line:#dfe3e6;--muted:#687078;--bg:#f5f7f8}*{box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans",sans-serif;margin:0;background:var(--bg);color:#202428}header{height:62px;background:#fff;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:16px;padding:0 24px}header h1{font-size:20px;margin:0}header a{margin-left:auto;color:#0f6e56;font-size:13px;text-decoration:none;border-bottom:1px solid #0f6e56}.page{padding:20px 24px}.tools{display:flex;align-items:center;gap:10px;margin:0 0 14px}.tools input,.tools select{height:38px;border:1px solid #bec5c9;border-radius:7px;background:#fff;padding:0 12px;font-size:14px}.tools input{width:280px}.count{color:var(--muted);font-size:13px}.card{background:#fff;border:1px solid var(--line);border-radius:9px;overflow:hidden}table{width:100%;border-collapse:collapse}th{background:#f8faf9;font-size:12px;text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);white-space:nowrap}td{border-bottom:1px solid #f0f2f3;padding:10px 12px;font-size:13px;vertical-align:top}tr:last-child td{border-bottom:0}.when{color:var(--muted);font-size:12px;white-space:nowrap}.who{white-space:nowrap}.actor{display:inline-block;font-size:10px;padding:2px 8px;border-radius:10px;margin-left:6px}.actor.student{background:#e6f1fb;color:#0c447c}.actor.teacher{background:#eaf3de;color:#27500a}.diff{margin:0}.diff div{margin-bottom:3px}.diff div:last-child{margin-bottom:0}.field{color:var(--muted);font-size:11px;margin-right:6px}.before{color:var(--muted);text-decoration:line-through}.arrow{color:var(--muted);margin:0 5px}.after{font-weight:700}.empty{padding:50px;text-align:center;color:var(--muted)}
</style></head><body><header><h1>更新履歴</h1><a href="/admin/carte">カルテに戻る</a></header><main class="page"><div class="tools"><input id="q" placeholder="生徒名・曲名で絞り込み"><select id="who"><option value="all">全員</option><option value="student">生徒の操作</option><option value="teacher">講師の操作</option></select><span class="count" id="count">読み込み中…</span></div><div class="card" id="list"><div class="empty">読み込み中…</div></div></main><script>
const esc=s=>String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const FIELDS={status:'状態',lesson_done:'実施',lesson_date:'授業日',student_note:'生徒メモ',teacher_note:'講師メモ',next_lesson:'次回レッスン'};
const STATUS={planned:'未実施',wanted:'やりたい',completed:'実施済み',practicing:'練習中',paused:'保留'};
function val(field,v){if(v===null||v===undefined||v==='')return '（なし）';if(field==='status')return STATUS[v]||v;if(typeof v==='boolean')return v?'あり':'なし';return String(v)}
function jaTime(iso){if(!iso)return '';let d=new Date(iso);if(isNaN(d))return iso;let p=n=>String(n).padStart(2,'0');return `${d.getFullYear()}/${p(d.getMonth()+1)}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`}
let items=[];
function draw(){let query=q.value.toLowerCase(),f=who.value;let xs=items.filter(x=>(x.name+' '+x.title).toLowerCase().includes(query)&&(f==='all'||x.actor===f));count.textContent=xs.length+'件';list.innerHTML=xs.length?`<table><thead><tr><th>日時</th><th>生徒</th><th>曲</th><th>変更内容</th></tr></thead><tbody>${xs.map(x=>`<tr><td class="when">${esc(jaTime(x.timestamp))}</td><td class="who">${esc(x.name)}<span class="actor ${x.actor==='teacher'?'teacher':'student'}">${x.actor==='teacher'?'講師':'生徒'}</span></td><td>${esc(x.title)}</td><td><div class="diff">${x.rows}</div></td></tr>`).join('')}</tbody></table>`:'<div class="empty">該当する記録はありません</div>'}
async function load(){let r=await fetch('/admin/carte/data'),d=await r.json();if(!r.ok)throw Error(d.error||'取得に失敗しました');let titles=Object.fromEntries(d.materials.map(m=>[m.id,m.title]));
 items=(d.history||[]).slice().reverse().map(h=>{let changed=h.changed||{};let rows=Object.keys(changed).map(k=>{let c=changed[k];return `<div><span class="field">${esc(FIELDS[k]||k)}</span><span class="before">${esc(val(k,c.before))}</span><span class="arrow">→</span><span class="after">${esc(val(k,c.after))}</span></div>`}).join('');
  return {timestamp:h.timestamp,name:h.display_name||h.user_id||'',title:titles[h.material_id]||('ID '+h.material_id),actor:h.actor||'',rows:rows||'<div>（変更なし）</div>'}});
 draw()}
q.oninput=draw;who.onchange=draw;load().catch(e=>{list.innerHTML='<div class="empty">'+esc(e.message)+'</div>'});
</script></body></html>"""


REQUESTS_HTML = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>リクエスト曲</title><style>
:root{--green:#087f5b;--amber:#a06a00;--line:#dfe3e6;--muted:#687078;--bg:#f5f7f8}*{box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans",sans-serif;margin:0;background:var(--bg);color:#202428}header{height:62px;background:#fff;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:16px;padding:0 24px}header h1{font-size:20px;margin:0}header a{margin-left:auto;color:#0f6e56;font-size:13px;text-decoration:none;border-bottom:1px solid #0f6e56}.page{padding:20px 24px}.tools{display:flex;align-items:center;gap:10px;margin:0 0 14px}.tools select{height:38px;border:1px solid #bec5c9;border-radius:7px;background:#fff;padding:0 12px;font-size:14px}.count{color:var(--muted);font-size:13px}.note{color:var(--muted);font-size:12px;margin:0 0 14px}.card{background:#fff;border:1px solid var(--line);border-radius:9px;overflow:auto}table{width:100%;border-collapse:collapse}th{background:#f8faf9;font-size:12px;text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);white-space:nowrap}td{border-bottom:1px solid #f0f2f3;padding:10px 12px;font-size:13px;vertical-align:top}tr:last-child td{border-bottom:0}.song b{display:block;font-size:14px}.song em{font-style:normal;color:var(--muted);font-size:11px}.tag{display:inline-block;font-size:10px;padding:2px 8px;border-radius:10px;margin-top:4px}.tag.uk{background:#e6f1fb;color:#0c447c}.tag.gt{background:#faece7;color:#712b13}.when{color:var(--muted);font-size:12px;white-space:nowrap}.who{white-space:nowrap;font-size:12px}.votes{text-align:center;font-weight:700;white-space:nowrap}.comment{color:#5f4a12;background:#fff8dc;border-radius:5px;padding:5px 8px;font-size:12px;display:inline-block}select.status{height:32px;border:1px solid #bec5c9;border-radius:6px;background:#fff;font-size:12px;padding:0 6px}button.del{height:32px;border:1px solid #e0bcbc;border-radius:6px;background:#fff;color:#a52b21;font-size:12px;padding:0 10px;cursor:pointer}.empty{padding:50px;text-align:center;color:var(--muted)}.notice{position:fixed;right:24px;bottom:20px;background:#183d31;color:#fff;padding:10px 16px;border-radius:7px;display:none}
</style></head><body><header><h1>リクエスト曲</h1><a href="/admin/carte">カルテに戻る</a></header><main class="page"><p class="note">生徒から届いた「やりたいけどまだリストに無い曲」です。シートに追加したら状態を「追加済み」にしてください。「見送り」にすると生徒側の一覧から消えます。</p><div class="tools"><select id="filter"><option value="open">未対応</option><option value="all">すべて</option><option value="added">追加済み</option><option value="declined">見送り</option></select><span class="count" id="count">読み込み中…</span></div><div class="card" id="list"><div class="empty">読み込み中…</div></div></main><div class="notice" id="notice">保存しました</div><script>
const esc=s=>String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const jaTime=iso=>{if(!iso)return '';let d=new Date(iso);if(isNaN(d))return iso;let p=n=>String(n).padStart(2,'0');return `${d.getFullYear()}/${p(d.getMonth()+1)}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`};
let items=[];
function draw(){let f=filter.value,xs=items.filter(x=>f==='all'||(x.status||'open')===f);count.textContent=xs.length+'件';list.innerHTML=xs.length?`<table><thead><tr><th>曲</th><th>ひとこと</th><th>リクエスト</th><th>私も</th><th>状態</th><th></th></tr></thead><tbody>${xs.map(x=>`<tr><td class="song"><b>${esc(x.title)}</b>${x.artist?`<em>${esc(x.artist)}</em>`:''}${x.instrument?`<br><span class="tag ${x.instrument==='ウクレレ'?'uk':'gt'}">${esc(x.instrument)}</span>`:''}</td><td>${x.comment?`<span class="comment">${esc(x.comment)}</span>`:''}</td><td class="who">${esc(x.display_name||'名前未登録')}<br><span class="when">${esc(jaTime(x.created_at))}</span></td><td class="votes">${(x.votes||[]).length}人</td><td><select class="status" onchange="setStatus('${esc(x.id)}',this.value)"><option value="open"${(x.status||'open')==='open'?' selected':''}>未対応</option><option value="added"${x.status==='added'?' selected':''}>追加済み</option><option value="declined"${x.status==='declined'?' selected':''}>見送り</option></select></td><td>${(x.status||'open')==='open'?`<a href="${new URL('admin/songs',location.origin+'/')}?request_id=${encodeURIComponent(x.id)}" style="display:inline-block;background:#087f5b;color:#fff;text-decoration:none;border-radius:6px;padding:8px 10px;font-size:12px;margin-right:5px">曲に登録</a>`:''}<button class="del" onclick="remove('${esc(x.id)}','${esc(x.title)}')">削除</button></td></tr>`).join('')}</tbody></table>`:'<div class="empty">該当するリクエストはありません</div>'}
function toast(t){notice.textContent=t;notice.style.display='block';setTimeout(()=>notice.style.display='none',2000)}
async function send(body){let r=await fetch('/admin/carte/request',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),d=await r.json();if(!r.ok)throw Error(d.error||'保存に失敗しました');await load()}
async function setStatus(id,status){try{await send({id,status});toast('状態を変えました')}catch(e){alert(e.message)}}
async function remove(id,title){if(!confirm('「'+title+'」を削除します。よろしいですか？'))return;try{await send({id,delete:true});toast('削除しました')}catch(e){alert(e.message)}}
async function load(){let r=await fetch('/admin/carte/data'),d=await r.json();if(!r.ok)throw Error(d.error||'取得に失敗しました');items=(d.requests||[]).slice().reverse();draw()}
filter.onchange=draw;load().catch(e=>{list.innerHTML='<div class="empty">'+esc(e.message)+'</div>'});
</script></body></html>"""


NEXT_LESSON_HTML = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>次回レッスンまとめ</title><style>
:root{--green:#087f5b;--line:#dfe3e6;--muted:#687078}*{box-sizing:border-box}body{margin:0;background:#f5f7f8;color:#202428;font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans",sans-serif}header{height:62px;background:#fff;border-bottom:1px solid var(--line);display:flex;align-items:center;padding:0 20px;gap:16px}header h1{font-size:20px;margin:0}header a{margin-left:auto;color:var(--green);text-decoration:none}.page{max-width:900px;margin:0 auto;padding:20px}.note{color:var(--muted);font-size:13px}.card{background:#fff;border:1px solid var(--line);border-radius:10px;padding:16px;margin:12px 0}.head{display:flex;align-items:center;gap:10px}.head h2{font-size:17px;margin:0}.count{color:var(--muted);font-size:12px}.send{margin-left:auto;border:0;border-radius:7px;background:var(--green);color:#fff;padding:10px 14px;font-weight:700;cursor:pointer}.send:disabled{opacity:.5}.song{border-top:1px solid #eef0f1;padding:11px 0}.song:first-of-type{margin-top:12px}.song b{font-size:14px}.song a{display:block;color:#a32d2d;font-size:12px;margin-top:4px;overflow:hidden;text-overflow:ellipsis}.memo{font-size:12px;color:#5f4a12;background:#fff8dc;border-radius:5px;padding:6px 8px;margin-top:5px}.empty{text-align:center;color:var(--muted);padding:50px}.notice{position:fixed;right:20px;bottom:20px;background:#183d31;color:#fff;padding:10px 16px;border-radius:7px;display:none}
</style></head><body><header><h1>次回レッスンまとめ</h1><a href="/admin/carte">カルテに戻る</a></header><main class="page"><p class="note">カルテで「次回レッスンでやる」にチェックした曲を、生徒ごとにまとめてLINE送信できます。</p><div id="list"><div class="empty">読み込み中…</div></div></main><div class="notice" id="notice"></div><script>
const esc=s=>String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let groups=[];
function draw(){list.innerHTML=groups.length?groups.map(g=>`<section class="card"><div class="head"><h2>${esc(g.display_name||'名前未登録')}</h2><span class="count">${g.items.length}曲</span><button class="send" onclick="sendNext('${esc(g.user_id)}',this)">LINEに送る</button></div>${g.items.map((x,i)=>`<div class="song"><b>${i+1}. ${esc(x.title)}${x.artist?' / '+esc(x.artist):''}</b>${x.video?`<a href="${esc(x.video)}" target="_blank" rel="noopener">${esc(x.video)}</a>`:''}${(x.teacher_note||x.student_note)?`<div class="memo">${esc(x.teacher_note||x.student_note)}</div>`:''}</div>`).join('')}</section>`).join(''):'<div class="empty">次回レッスン曲はまだ設定されていません</div>'}
async function load(){let r=await fetch('/admin/carte/next/data'),d=await r.json();if(!r.ok)throw Error(d.error||'取得に失敗しました');groups=d.groups||[];draw()}
async function sendNext(uid,b){let g=groups.find(x=>x.user_id===uid);if(!g||!confirm((g.display_name||'この生徒')+'さんへ次回レッスン曲を送りますか？'))return;b.disabled=true;try{let r=await fetch('/admin/carte/next/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_id:uid})}),d=await r.json();if(!r.ok)throw Error(d.error||'送信に失敗しました');notice.textContent='LINEに送信しました';notice.style.display='block';setTimeout(()=>notice.style.display='none',2200)}catch(e){alert(e.message)}finally{b.disabled=false}}
load().catch(e=>list.innerHTML='<div class="empty">'+esc(e.message)+'</div>');
</script></body></html>"""


ADMIN_LOGIN_HTML = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>講師ログイン</title><style>
*{box-sizing:border-box}body{margin:0;background:#f5f7f8;color:#202428;font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans",sans-serif;min-height:100vh;display:grid;place-items:center}.box{width:min(420px,calc(100% - 32px));background:#fff;border:1px solid #dfe3e6;border-radius:12px;padding:30px;box-shadow:0 8px 30px rgba(20,40,30,.08)}h1{font-size:22px;margin:0 0 8px}.sub{color:#687078;font-size:14px;margin:0 0 24px}label{display:block;font-size:13px;font-weight:700;margin-bottom:7px}input{width:100%;height:46px;border:1px solid #bcc3c7;border-radius:7px;padding:0 12px;font-size:17px}button{width:100%;height:46px;border:0;border-radius:7px;background:#087f5b;color:#fff;font-size:16px;font-weight:700;margin-top:16px;cursor:pointer}.error{background:#fff1f0;color:#a52b21;border-radius:6px;padding:10px 12px;font-size:13px;margin-bottom:16px}
</style></head><body><main class="box"><h1>講師用カルテ</h1><p class="sub">管理用の合言葉を入力してください。</p>__ERROR__<form method="post" action="/admin/carte/login"><label for="password">合言葉</label><input id="password" name="password" type="password" autocomplete="current-password" autofocus required><button type="submit">ログイン</button></form></main></body></html>"""


def render_student_page(liff_id):
    return STUDENT_HTML.replace("__LIFF_ID__", str(liff_id))


def create_carte_blueprint(
    verify_liff_user, upsert_member, admin_token, liff_id, push_text=None
):
    bp = Blueprint("carte", __name__)

    def admin_cookie_value():
        token = str(admin_token)
        return hmac.new(
            token.encode("utf-8"), b"carte-admin-login", hashlib.sha256
        ).hexdigest()

    def is_admin():
        from admin_auth import teacher_session_ok

        if teacher_session_ok():
            return True
        token = str(admin_token)
        if not token:
            return False
        supplied = request.args.get("token", "")
        cookie = request.cookies.get("carte_admin", "")
        return hmac.compare_digest(supplied, token) or hmac.compare_digest(
            cookie, admin_cookie_value()
        )

    def require_admin():
        if not is_admin():
            abort(403)

    @bp.get("/carte")
    @bp.get("/liff/carte")
    def student_page():
        return render_student_page(liff_id)

    @bp.post("/api/carte/me")
    def my_carte():
        body = request.get_json(silent=True) or {}
        user_id, name = verify_liff_user(body.get("idToken", ""))
        if not user_id:
            return {"error": name}, 401
        _upsert_carte_member(user_id, name)
        # 曲は全部返し、画面側で楽器タブを切り替える。
        # 講師が設定した楽器は「最初に選ばれるタブ」として使う（固定はしない）。
        materials = load_materials()
        instrument = _prefs().get(user_id, {}).get("instrument", "")
        return {
            "display_name": name,
            "instrument": instrument,
            "materials": materials,
            "progress": _student_rows(user_id),
            "popular": _popular_counts(),
            "requests": _public_requests(user_id),
        }

    @bp.post("/api/carte/request")
    def add_request():
        """生徒がリクエスト曲を送る。"""
        body = request.get_json(silent=True) or {}
        user_id, name = verify_liff_user(body.get("idToken", ""))
        if not user_id:
            return {"error": name}, 401
        title = str(body.get("title") or "").strip()[:120]
        if not title:
            return {"error": "曲名を入力してください。"}, 400
        normalized = _normalized_title(title)
        duplicate_material = next(
            (item for item in load_materials() if _normalized_title(item.get("title", "")) == normalized),
            None,
        )
        if duplicate_material:
            return {"error": f"「{duplicate_material['title']}」は曲リストに登録済みです。検索してご利用ください。"}, 400
        rows = _requests()
        duplicate_request = next(
            (
                row
                for row in rows
                if row.get("status") != "declined"
                and _normalized_title(row.get("title", "")) == normalized
            ),
            None,
        )
        if duplicate_request:
            return {"error": "同じ曲がすでにリクエストされています。「私も」を押してください。"}, 400
        if sum(1 for r in rows if r.get("user_id") == user_id and r.get("status") == "open") >= 20:
            return {"error": "リクエストがたまっています。先生の対応を待ってから追加してください。"}, 400
        instrument = str(body.get("instrument") or "").strip()
        if instrument not in {"", "ウクレレ", "ギター"}:
            instrument = ""
        rows.append(
            {
                "id": f"{int(time.time() * 1000)}-{user_id[-6:]}",
                "user_id": user_id,
                "display_name": name,
                "title": title,
                "artist": str(body.get("artist") or "").strip()[:120],
                "instrument": instrument,
                "comment": str(body.get("comment") or "").strip()[:300],
                "status": "open",
                "votes": [],
                "created_at": _now(),
            }
        )
        save_json("carte:requests", rows[-500:])
        return {"ok": True, "requests": _public_requests(user_id)}

    @bp.post("/api/carte/request/vote")
    def vote_request():
        """「私も」の付け外し。"""
        body = request.get_json(silent=True) or {}
        user_id, name = verify_liff_user(body.get("idToken", ""))
        if not user_id:
            return {"error": name}, 401
        request_id = str(body.get("id") or "")
        rows = _requests()
        row = next((r for r in rows if r.get("id") == request_id), None)
        if not row or row.get("status") == "declined":
            return {"error": "リクエストが見つかりません。"}, 404
        votes = [v for v in row.get("votes", []) if v != user_id]
        if not row.get("votes") or user_id not in row.get("votes", []):
            votes.append(user_id)
        row["votes"] = votes
        save_json("carte:requests", rows)
        return {"ok": True, "requests": _public_requests(user_id)}

    @bp.post("/api/carte/progress")
    def student_progress():
        body = request.get_json(silent=True) or {}
        user_id, name = verify_liff_user(body.get("idToken", ""))
        if not user_id:
            return {"error": name}, 401
        try:
            material_id = int(body.get("material_id"))
        except (TypeError, ValueError):
            return {"error": "教材IDが正しくありません。"}, 400
        row, error = _upsert_progress(user_id, name, material_id, body, "student")
        return ({"error": error}, 400) if error else {"ok": True, "progress": row}

    @bp.get("/admin/carte")
    def admin_page():
        if not is_admin():
            return ADMIN_LOGIN_HTML.replace("__ERROR__", "")
        if request.args.get("token"):
            response = make_response(redirect("/admin/carte"))
            response.set_cookie(
                "carte_admin", admin_cookie_value(), max_age=60 * 60 * 24 * 30,
                secure=True, httponly=True, samesite="Strict"
            )
            return response
        return ADMIN_HTML

    @bp.get("/admin/carte/ranking")
    def admin_ranking():
        """曲ごとの「実施済み」「やりたい」の人数ランキング（講師のみ）。
        集計は /admin/carte/data の結果をブラウザ側で数えるだけなので、サーバー側の処理は増えない。"""
        require_admin()
        return RANKING_HTML

    @bp.post("/admin/carte/login")
    def admin_login():
        from admin_auth import password_ok, set_teacher_cookie

        password = request.form.get("password", "")
        token = str(admin_token)
        shared_ok = password_ok(password)
        tenant_ok = bool(token) and hmac.compare_digest(password, token)
        if not shared_ok and not tenant_ok:
            error = '<div class="error">合言葉が違います。もう一度お試しください。</div>'
            return ADMIN_LOGIN_HTML.replace("__ERROR__", error), 401
        response = make_response(redirect("/admin/carte"))
        if shared_ok:
            set_teacher_cookie(response)
        else:
            response.set_cookie(
                "carte_admin", admin_cookie_value(), max_age=60 * 60 * 24 * 30,
                secure=True, httponly=True, samesite="Strict"
            )
        return response

    @bp.post("/admin/carte/logout")
    def admin_logout():
        from admin_auth import clear_teacher_cookie

        response = make_response(redirect("/admin/carte"))
        response.delete_cookie("carte_admin", secure=True, httponly=True, samesite="Strict")
        clear_teacher_cookie(response)
        return response

    @bp.get("/admin/carte/data")
    def admin_data():
        require_admin()
        members = _carte_members()
        rows = _progress()
        prefs = _prefs()
        students = []
        for member in members:
            mine = [r for r in rows if r.get("user_id") == member.get("user_id")]
            students.append(
                {
                    **member,
                    "instrument": prefs.get(member.get("user_id"), {}).get("instrument", ""),
                    "wanted_count": sum(r.get("status") == "wanted" for r in mine),
                    "practicing_count": sum(r.get("status") == "practicing" for r in mine),
                    "completed_count": sum(r.get("status") == "completed" for r in mine),
                    "next_count": sum(bool(r.get("next_lesson")) for r in mine),
                    "updated_at": max((r.get("updated_at", "") for r in mine), default=""),
                }
            )
        return {
            "students": students,
            "materials": load_materials(),
            "progress": rows,
            "history": _history()[-200:],
            "requests": _requests(),
        }

    @bp.post("/admin/carte/request")
    def update_request():
        """リクエストの状態を変える／削除する（講師のみ）。"""
        require_admin()
        body = request.get_json(silent=True) or {}
        request_id = str(body.get("id") or "")
        rows = _requests()
        row = next((r for r in rows if r.get("id") == request_id), None)
        if not row:
            return {"error": "リクエストが見つかりません。"}, 404
        if body.get("delete"):
            rows = [r for r in rows if r.get("id") != request_id]
        else:
            status = str(body.get("status") or "")
            if status not in {"open", "added", "declined"}:
                return {"error": "状態が正しくありません。"}, 400
            row["status"] = status
        save_json("carte:requests", rows)
        return {"ok": True}

    @bp.get("/admin/carte/requests")
    def admin_requests():
        require_admin()
        return REQUESTS_HTML

    @bp.post("/admin/carte/progress")
    def admin_progress():
        require_admin()
        body = request.get_json(silent=True) or {}
        user_id = str(body.get("user_id") or "")
        member = next((m for m in _carte_members() if m.get("user_id") == user_id), None)
        if not member:
            return {"error": "生徒が見つかりません。"}, 404
        try:
            material_id = int(body.get("material_id"))
        except (TypeError, ValueError):
            return {"error": "教材IDが正しくありません。"}, 400
        row, error = _upsert_progress(
            user_id, member.get("display_name", user_id), material_id, body, "teacher"
        )
        return ({"error": error}, 400) if error else {"ok": True, "progress": row}

    @bp.post("/admin/carte/student")
    def set_student_instrument():
        """生徒ごとの楽器を設定する。設定すると、その生徒の画面にはその楽器の曲だけが出る。"""
        require_admin()
        body = request.get_json(silent=True) or {}
        user_id = str(body.get("user_id") or "")
        if not any(m.get("user_id") == user_id for m in _carte_members()):
            return {"error": "生徒が見つかりません。"}, 404
        instrument = str(body.get("instrument") or "").strip()
        if instrument not in {"", "ウクレレ", "ギター"}:
            return {"error": "楽器が正しくありません。"}, 400
        prefs = _prefs()
        prefs.setdefault(user_id, {})["instrument"] = instrument
        save_json("carte:prefs", prefs)
        return {"ok": True, "instrument": instrument}

    @bp.get("/admin/carte/history")
    def admin_history():
        """誰がいつ何を変えたかの一覧（講師のみ）。"""
        require_admin()
        return HISTORY_HTML

    @bp.get("/admin/carte/next")
    def admin_next_lesson():
        require_admin()
        return NEXT_LESSON_HTML

    @bp.get("/admin/carte/next/data")
    def admin_next_lesson_data():
        require_admin()
        return {"groups": next_lesson_groups()}

    @bp.post("/admin/carte/next/send")
    def admin_next_lesson_send():
        require_admin()
        if push_text is None:
            return {"error": "LINE送信機能が設定されていません。"}, 503
        body = request.get_json(silent=True) or {}
        user_id = str(body.get("user_id") or "")
        group = next(
            (item for item in next_lesson_groups() if item.get("user_id") == user_id),
            None,
        )
        if not group:
            return {"error": "次回レッスン曲が見つかりません。"}, 404
        message = build_next_lesson_message(group)
        try:
            push_text(user_id, message)
        except Exception as exc:
            return {"error": f"LINE送信に失敗しました: {str(exc)[:240]}"}, 502
        notifications = load_json("carte:notifications", default=[])
        if not isinstance(notifications, list):
            notifications = []
        notifications.append(
            {
                "user_id": user_id,
                "display_name": group.get("display_name", ""),
                "material_ids": [item["id"] for item in group.get("items", [])],
                "sent_at": _now(),
            }
        )
        save_json("carte:notifications", notifications[-500:])
        return {"ok": True, "sent_at": notifications[-1]["sent_at"]}

    @bp.post("/admin/carte/sync")
    def sync_materials():
        require_admin()
        return {"ok": True, "count": len(load_materials(force=True))}

    return bp
