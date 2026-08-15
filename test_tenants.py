import json
import base64
import hashlib
import hmac
from pathlib import Path

import pytest


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("MASTER_ADMIN_TOKEN", "master-admin")
    monkeypatch.setenv("KANSAI_CHANNEL_ACCESS_TOKEN", "kansai-token")
    monkeypatch.setenv("KANSAI_CHANNEL_SECRET", "kansai-secret")
    monkeypatch.setenv("KANSAI_ADMIN_TOKEN", "kansai-admin")
    monkeypatch.setenv("KANSAI_LIFF_ID", "kansai-liff")
    monkeypatch.setenv("KANSAI_LINE_CHANNEL_ID", "111")
    monkeypatch.setenv("KANSAI_PANEL_NAME", "関西")
    monkeypatch.setenv("KANSAI_SCHEDULE_ENABLED", "true")
    monkeypatch.setenv("KANSAI_CARTE_ENABLED", "false")
    monkeypatch.setenv("KANTO_CHANNEL_ACCESS_TOKEN", "kanto-token")
    monkeypatch.setenv("KANTO_CHANNEL_SECRET", "kanto-secret")
    monkeypatch.setenv("KANTO_ADMIN_TOKEN", "kanto-admin")
    monkeypatch.setenv("KANTO_CARTE_LIFF_ID", "kanto-carte")
    monkeypatch.setenv("KANTO_LINE_CHANNEL_ID", "222")
    monkeypatch.setenv("KANTO_PANEL_NAME", "関東")
    monkeypatch.setenv("KANTO_SCHEDULE_ENABLED", "false")
    monkeypatch.setenv("KANTO_CARTE_ENABLED", "true")

    import importlib
    import tenant_config
    import storage
    import app

    importlib.reload(tenant_config)
    importlib.reload(storage)
    importlib.reload(app)
    monkeypatch.setattr(storage, "BASE_DIR", tmp_path)
    return app.app.test_client(), storage, tmp_path


def test_feature_routes_are_tenant_scoped(client):
    http, _, _ = client
    assert http.get("/kansai/liff").status_code == 200
    assert http.get("/kansai/carte").status_code == 404
    assert http.get("/kanto/liff").status_code == 404
    assert http.get("/kanto/carte").status_code == 200


def test_carte_pages_embed_youtube_videos(client):
    http, _, _ = client
    student_html = http.get("/kanto/carte").get_data(as_text=True)

    import carte

    for html in (student_html, carte.ADMIN_HTML):
        assert 'id="videoDialog"' in html
        assert 'id="videoFrame"' in html
        assert "function youtubeId(url)" in html
        assert "www.youtube-nocookie.com/embed/" in html
        assert "playVideo(event,this.dataset.video)" in html
        assert "再生できない場合はYouTubeで開く" in html


def test_admin_page_keeps_tenant_in_links(client):
    http, _, _ = client
    response = http.get("/kansai/admin/panel?token=kansai-admin")
    assert response.status_code == 200
    assert "関西 管理画面" in response.get_data(as_text=True)
    assert "'/kansai/admin/" in response.get_data(as_text=True)


def test_shared_teacher_login_opens_all_admin_pages(client):
    http, _, _ = client
    secure = {"base_url": "https://localhost"}

    response = http.post(
        "/admin/login", data={"password": "master-admin"}, **secure
    )
    assert response.status_code == 302
    home = http.get("/admin", **secure)
    html = home.get_data(as_text=True)
    assert home.status_code == 200
    assert "/kansai/admin/panel" in html
    assert "/kanto/admin/panel" in html
    assert "/kanto/admin/carte" in html

    panel = http.get("/kansai/admin/panel", **secure)
    assert panel.status_code == 200
    assert "kansai-admin" not in panel.get_data(as_text=True)
    assert http.get("/kanto/admin/carte", **secure).status_code == 200


def test_shared_teacher_login_rejects_wrong_password(client):
    http, _, _ = client
    response = http.post(
        "/admin/login",
        data={"password": "wrong"},
        base_url="https://localhost",
    )
    assert response.status_code == 401


def test_teacher_can_add_custom_song_without_sheet_writer(client, monkeypatch):
    http, storage, tmp_path = client
    secure = {"base_url": "https://localhost"}
    http.post("/admin/login", data={"password": "master-admin"}, **secure)

    import carte

    monkeypatch.setattr(carte, "load_materials", lambda force=False: [])
    response = http.post(
        "/admin/songs",
        data={
            "title": "新しい曲",
            "instrument": "ウクレレ",
            "kind": "弾き語り",
            "video": "https://youtu.be/example123",
        },
        **secure,
    )
    assert response.status_code == 302
    rows = json.loads(
        (Path(tmp_path) / "kanto_carte_custom_materials.json").read_text()
    )
    assert rows[0]["title"] == "新しい曲"
    assert rows[0]["id"] >= 1000000


def test_song_entry_rejects_duplicate_youtube_alias(client, monkeypatch):
    http, _, _ = client
    secure = {"base_url": "https://localhost"}
    http.post("/admin/login", data={"password": "master-admin"}, **secure)

    import carte

    monkeypatch.setattr(
        carte,
        "load_materials",
        lambda force=False: [{
            "id": 10,
            "title": "登録済み",
            "video": "https://www.youtube.com/watch?v=abc123",
        }],
    )
    response = http.post(
        "/admin/songs",
        data={"title": "別名", "video": "https://youtu.be/abc123"},
        **secure,
    )
    assert response.status_code == 400
    assert "同じ動画URL" in response.get_data(as_text=True)


def test_teacher_song_entry_writes_to_google_sheet_when_configured(client, monkeypatch):
    http, _, _ = client
    secure = {"base_url": "https://localhost"}
    http.post("/admin/login", data={"password": "master-admin"}, **secure)

    import carte

    monkeypatch.setenv("REPERTOIRE_SHEET_WRITE_URL", "https://script.example/exec")
    monkeypatch.setenv("REPERTOIRE_SHEET_WRITE_SECRET", "writer-secret")
    monkeypatch.setattr(carte, "load_materials", lambda force=False: [])

    sent = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True, "id": 430}

    def post(url, json, timeout):
        sent.update(url=url, payload=json, timeout=timeout)
        return Response()

    monkeypatch.setattr(carte.requests, "post", post)
    response = http.post(
        "/admin/songs",
        data={
            "title": "シートへ追加する曲",
            "instrument": "ウクレレ",
            "kind": "弾き語り",
            "artist": "歌手",
            "video": "https://youtu.be/new-sheet-song",
            "note": "メモ",
            "genre": "ポップス",
        },
        **secure,
    )

    assert response.status_code == 302
    assert sent["url"] == "https://script.example/exec"
    assert sent["timeout"] == 20
    assert sent["payload"]["secret"] == "writer-secret"
    assert sent["payload"]["genre"] == "ポップス"
    assert "Google+Sheet" in response.headers["Location"]


def test_next_lesson_summary_can_be_sent(client, monkeypatch):
    http, storage, _ = client
    secure = {"base_url": "https://localhost"}
    http.post("/admin/login", data={"password": "master-admin"}, **secure)

    import app
    import carte
    from flask import g

    monkeypatch.setattr(
        carte,
        "load_materials",
        lambda force=False: [{
            "id": 50,
            "title": "次回の曲",
            "artist": "",
            "video": "https://youtu.be/next123",
        }],
    )
    with http.application.test_request_context("/kanto/carte"):
        g.tenant = "kanto"
        storage.save_json(
            "carte:members", [{"user_id": "student-1", "display_name": "生徒A"}]
        )
        storage.save_json(
            "carte:progress",
            [{
                "user_id": "student-1",
                "display_name": "生徒A",
                "material_id": 50,
                "next_lesson": True,
                "student_note": "練習メモ",
            }],
        )

    sent = []
    monkeypatch.setattr(app, "push_text_message", lambda uid, text: sent.append((uid, text)))
    data = http.get("/kanto/admin/carte/next/data", **secure).get_json()
    assert data["groups"][0]["items"][0]["title"] == "次回の曲"

    response = http.post(
        "/kanto/admin/carte/next/send",
        json={"user_id": "student-1"},
        **secure,
    )
    assert response.status_code == 200
    assert sent[0][0] == "student-1"
    assert "次回の曲" in sent[0][1]


def test_teacher_can_create_recoverable_snapshot(client):
    http, _, tmp_path = client
    secure = {"base_url": "https://localhost"}
    http.post("/admin/login", data={"password": "master-admin"}, **secure)
    response = http.post("/admin/maintenance/backup", **secure)
    assert response.status_code == 302
    path = Path(tmp_path) / "kanto_carte_backups.json"
    assert path.exists()
    snapshots = json.loads(path.read_text())
    assert snapshots[-1]["schedules"]["kansai"] is not None
    assert snapshots[-1]["carte"] is not None


def test_teacher_can_open_maintenance_status(client, monkeypatch):
    http, _, _ = client
    secure = {"base_url": "https://localhost"}
    http.post("/admin/login", data={"password": "master-admin"}, **secure)

    import maintenance

    monkeypatch.setattr(
        maintenance,
        "system_status",
        lambda: {
            "storage": {"ok": True, "backend": "upstash", "error": ""},
            "sheet": {"ok": True, "count": 429, "error": ""},
            "tenants": {
                "kansai": {
                    "line_configured": True,
                    "schedule_enabled": True,
                    "carte_enabled": False,
                },
                "kanto": {
                    "line_configured": True,
                    "schedule_enabled": False,
                    "carte_enabled": True,
                },
            },
            "keepalive": {"configured": False, "enabled": False},
            "backup": {"count": 1, "latest_at": "2026-08-16T00:00:00Z"},
        },
    )
    response = http.get("/admin/maintenance", **secure)
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "429曲" in html
    assert "1世代" in html
    assert "関西 LINE" in html


def test_storage_is_isolated_by_tenant(client):
    http, storage, tmp_path = client
    with http.application.test_request_context("/kansai/liff"):
        from flask import g
        g.tenant = "kansai"
        storage.save_json("members", [{"user_id": "west"}])
    with http.application.test_request_context("/kanto/carte"):
        from flask import g
        g.tenant = "kanto"
        storage.save_json("members", [{"user_id": "east"}])

    assert json.loads((Path(tmp_path) / "members.json").read_text()) == [{"user_id": "west"}]
    assert json.loads((Path(tmp_path) / "kanto_members.json").read_text()) == [{"user_id": "east"}]


def test_carte_storage_is_shared_across_tenants(client):
    http, storage, tmp_path = client
    with http.application.test_request_context("/kansai/carte"):
        from flask import g
        g.tenant = "kansai"
        storage.save_json("carte:progress", [{"user_id": "shared"}])
    with http.application.test_request_context("/kanto/carte"):
        from flask import g
        g.tenant = "kanto"
        assert storage.load_json("carte:progress") == [{"user_id": "shared"}]

    assert (Path(tmp_path) / "kanto_carte_progress.json").exists()


def test_unknown_tenant_is_rejected(client):
    http, _, _ = client
    assert http.post("/webhook/unknown").status_code == 404


@pytest.mark.parametrize(
    ("tenant", "secret"),
    [("kansai", "kansai-secret"), ("kanto", "kanto-secret")],
)
def test_each_webhook_uses_its_own_secret(client, tenant, secret):
    http, _, _ = client
    body = b'{"events":[]}'
    signature = base64.b64encode(
        hmac.new(secret.encode(), body, hashlib.sha256).digest()
    ).decode()
    assert http.post(
        f"/webhook/{tenant}",
        data=body,
        headers={"X-Line-Signature": signature, "Content-Type": "application/json"},
    ).status_code == 200

    wrong = base64.b64encode(
        hmac.new(b"wrong-secret", body, hashlib.sha256).digest()
    ).decode()
    assert http.post(
        f"/webhook/{tenant}",
        data=body,
        headers={"X-Line-Signature": wrong, "Content-Type": "application/json"},
    ).status_code == 400


def test_song_edit_and_archive_keep_the_same_id(client, monkeypatch):
    http, _, _ = client
    import carte

    current = {
        "id": 430,
        "title": "元の曲名",
        "artist": "",
        "instrument": "ウクレレ",
        "kind": "弾き語り",
        "video": "",
        "note": "",
        "genre": "",
        "active": True,
        "source": "sheet",
    }
    monkeypatch.setenv("REPERTOIRE_SHEET_WRITE_URL", "https://script.example/exec")
    monkeypatch.setenv("REPERTOIRE_SHEET_WRITE_SECRET", "writer-secret")
    monkeypatch.setattr(
        carte,
        "load_materials",
        lambda force=False, include_inactive=False: [dict(current)],
    )
    payloads = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True, "id": 430}

    monkeypatch.setattr(
        carte.requests,
        "post",
        lambda url, json, timeout: (payloads.append(dict(json)) or Response()),
    )
    with http.application.test_request_context("/kanto/carte"):
        from flask import g

        g.tenant = "kanto"
        item, error = carte.update_material(
            430,
            {**current, "title": "変更後の曲名"},
            "update",
        )
        assert not error
        assert item["id"] == 430
        archived, error = carte.update_material(430, action="archive")
        assert not error
        assert archived["id"] == 430
        assert archived["active"] is False
    assert payloads[0]["action"] == "update"
    assert payloads[0]["id"] == 430
    assert payloads[1]["action"] == "archive"


def test_schedule_conflict_is_detected_across_regions(client):
    http, storage, _ = client
    from flask import g
    from lesson_operations import cross_tenant_conflicts

    with http.application.test_request_context("/kanto/admin/panel"):
        g.tenant = "kanto"
        storage.save_json(
            "assignment",
            [{"day": "2026-08-20", "time": "13:00", "end": "14:00", "name": "関東A"}],
        )
        g.tenant = "kansai"
        problems = cross_tenant_conflicts(
            "kansai",
            [{"day": "2026-08-20", "time": "13:30", "end": "14:30", "name": "関西B"}],
        )
    assert problems
    assert "関東" in problems[0]


def test_reminder_preview_targets_only_nonrespondents(client):
    http, storage, _ = client
    secure = {"base_url": "https://localhost"}
    http.post("/admin/login", data={"password": "master-admin"}, **secure)
    from flask import g

    with http.application.test_request_context("/kansai/admin/panel"):
        g.tenant = "kansai"
        storage.save_json(
            "members",
            [
                {"user_id": "answered", "display_name": "回答済み"},
                {"user_id": "waiting", "display_name": "未回答"},
            ],
        )
        storage.save_json(
            "votes",
            [{"user_id": "answered", "display_name": "回答済み", "candidate_index": 1}],
        )
        storage.save_json("schedule_targets", ["answered", "waiting"])
    page = http.get("/admin/reminders/kansai", **secure)
    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "未回答" in html
    assert "未回答者リマインド（1人）" in html


def test_full_backup_can_be_restored_with_a_safety_copy(client):
    http, storage, tmp_path = client
    secure = {"base_url": "https://localhost"}
    http.post("/admin/login", data={"password": "master-admin"}, **secure)
    from flask import g
    import maintenance

    with http.application.test_request_context("/kansai/admin/panel"):
        g.tenant = "kansai"
        storage.save_json("members", [{"user_id": "before"}])
        first = maintenance.save_snapshot()
        storage.save_json("members", [{"user_id": "after"}])
        result = maintenance.restore_snapshot(0)
        assert storage.load_json("members")[0]["user_id"] == "before"
    snapshots = json.loads((Path(tmp_path) / "kanto_carte_backups.json").read_text())
    assert len(snapshots) == 2
    assert result["restored_at"] == first["created_at"]


def test_student_carte_has_tap_summary_filters(client):
    http, _, _ = client
    html = http.get("/kanto/carte").get_data(as_text=True)
    assert "function renderSummary()" in html
    assert "function chooseSummary(value)" in html
    assert "次回" in html


def test_teacher_can_sync_each_region_to_google_calendar(client, monkeypatch):
    http, storage, tmp_path = client
    secure = {"base_url": "https://localhost"}
    http.post("/admin/login", data={"password": "master-admin"}, **secure)
    monkeypatch.setenv("REPERTOIRE_SHEET_WRITE_URL", "https://script.example/exec")
    monkeypatch.setenv("REPERTOIRE_SHEET_WRITE_SECRET", "writer-secret")

    from flask import g
    import lesson_operations

    with http.application.test_request_context("/kansai/admin/panel"):
        g.tenant = "kansai"
        storage.save_json(
            "assignment",
            [{
                "day": "2099-08-20",
                "time": "13:00",
                "end": "14:00",
                "name": "生徒A",
                "location": "梅田教室",
                "member_ids": ["student-a"],
            }],
        )

    sent = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            event = sent["payload"]["events"][0]
            return {
                "ok": True,
                "calendar_id": "kansai-calendar@example.com",
                "calendar_name": "Lesson 関西 日程",
                "created": 1,
                "updated": 0,
                "deleted": 0,
                "records": [{**event, "event_id": "event-1"}],
            }

    def post(url, json, timeout):
        sent.update(url=url, payload=json, timeout=timeout)
        return Response()

    monkeypatch.setattr(lesson_operations.requests, "post", post)
    response = http.post("/admin/calendar/kansai", **secure)
    assert response.status_code == 302
    assert sent["payload"]["action"] == "calendar_sync"
    assert sent["payload"]["tenant"] == "kansai"
    assert sent["payload"]["secret"] == "writer-secret"
    assert sent["payload"]["events"][0]["title"] == "【関西】レッスン：生徒A"
    assert sent["payload"]["events"][0]["start"].startswith("2099-08-20T13:00")

    state = json.loads((Path(tmp_path) / "calendar_sync.json").read_text())
    assert state["calendar_name"] == "Lesson 関西 日程"
    assert state["records"][0]["event_id"] == "event-1"

    page = http.get("/admin/calendar", **secure)
    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "Lesson 関西 日程" in html
    assert "同期済み 1件" in html
    assert "Lesson 関東 日程" in html


def test_calendar_sync_deletes_removed_future_events(client, monkeypatch):
    http, storage, _ = client
    monkeypatch.setenv("REPERTOIRE_SHEET_WRITE_URL", "https://script.example/exec")
    monkeypatch.setenv("REPERTOIRE_SHEET_WRITE_SECRET", "writer-secret")
    from flask import g
    import lesson_operations

    with http.application.test_request_context("/kansai/admin/panel"):
        g.tenant = "kansai"
        storage.save_json("assignment", [])
        storage.save_json(
            "calendar_sync",
            {
                "records": [{
                    "key": "old-key",
                    "event_id": "old-event",
                    "start": "2099-08-20T13:00:00+09:00",
                }]
            },
        )

    sent = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "ok": True,
                "calendar_name": "Lesson 関西 日程",
                "records": [],
                "deleted": 1,
            }

    monkeypatch.setattr(
        lesson_operations.requests,
        "post",
        lambda url, json, timeout: (sent.update(payload=json) or Response()),
    )
    result = lesson_operations.sync_calendar_schedule("kansai")
    assert result["ok"] is True
    assert result["deleted"] == 1
    assert sent["payload"]["delete_event_ids"] == ["old-event"]


def test_teacher_can_open_new_management_pages(client):
    http, _, _ = client
    secure = {"base_url": "https://localhost"}
    http.post("/admin/login", data={"password": "master-admin"}, **secure)
    for path, label in (
        ("/admin/automations", "自動通知"),
        ("/admin/lessons", "変更・キャンセル"),
        ("/admin/attendance?month=2026-08", "出欠・月謝"),
    ):
        response = http.get(path, **secure)
        assert response.status_code == 200
        assert label in response.get_data(as_text=True)


def test_automatic_tomorrow_reminder_is_not_sent_twice(client):
    _, storage, _ = client
    from datetime import datetime
    from zoneinfo import ZoneInfo
    import lesson_operations

    with lesson_operations.tenant_scope("kansai"):
        storage.save_json(
            "members", [{"user_id": "student-1", "display_name": "生徒A"}]
        )
        storage.save_json(
            "assignment",
            [{
                "day": "2026-08-17",
                "time": "13:00",
                "end": "14:00",
                "name": "生徒A",
                "member_ids": ["student-1"],
            }],
        )
    sent = []
    now = datetime(2026, 8, 16, 18, 5, tzinfo=ZoneInfo("Asia/Tokyo"))
    lesson_operations.run_due_automations(lambda uid, text: sent.append((uid, text)), now=now)
    lesson_operations.run_due_automations(lambda uid, text: sent.append((uid, text)), now=now)
    assert [uid for uid, _ in sent] == ["student-1"]
    assert "明日のレッスン" in sent[0][1]


def test_lesson_reschedule_updates_attendance_and_cancel_keeps_history(client):
    _, storage, _ = client
    import lesson_operations

    with lesson_operations.tenant_scope("kansai"):
        storage.save_json(
            "members", [{"user_id": "student-1", "display_name": "生徒A"}]
        )
        storage.save_json(
            "assignment",
            [{
                "day": "2099-08-20",
                "time": "13:00",
                "end": "14:00",
                "name": "生徒A",
                "location": "梅田",
                "member_ids": ["student-1"],
            }],
        )
    lesson_id = lesson_operations.ensure_lesson_ids("kansai")[0]["lesson_id"]
    result = lesson_operations.update_lesson(
        "kansai",
        lesson_id,
        {"day": "2099-08-21", "time": "14:00", "end": "15:00", "location": "難波"},
        notify=False,
    )
    assert result["ok"] is True
    with lesson_operations.tenant_scope("kansai"):
        assert storage.load_json("assignment")[0]["day"] == "2099-08-21"
    with lesson_operations.tenant_scope("kanto"):
        attendance = storage.load_json("carte:attendance")
    assert attendance[0]["day"] == "2099-08-21"
    lesson_operations.cancel_lesson("kansai", lesson_id, notify=False)
    with lesson_operations.tenant_scope("kansai"):
        assert storage.load_json("assignment") == []
    with lesson_operations.tenant_scope("kanto"):
        assert storage.load_json("carte:attendance")[0]["status"] == "cancelled"


def test_tuition_and_student_home_data_are_shared(client):
    _, storage, _ = client
    import carte
    import lesson_operations

    with lesson_operations.tenant_scope("kansai"):
        storage.save_json(
            "assignment",
            [{
                "day": "2099-09-01",
                "time": "10:00",
                "end": "11:00",
                "location": "梅田",
                "name": "生徒A",
                "member_ids": ["student-1"],
            }],
        )
    progress = [{
        "user_id": "student-1",
        "material_id": 10,
        "next_lesson": True,
        "teacher_note": "サビをゆっくり練習しましょう",
    }]
    home = carte._student_home(
        "student-1", [{"id": 10, "title": "次回の曲", "artist": "歌手"}], progress
    )
    assert home["next_lesson"]["label"] == "関西"
    assert home["next_songs"][0]["title"] == "次回の曲"
    assert "ゆっくり" in home["teacher_notes"][0]

    saved = lesson_operations.save_tuition_record(
        "student-1", "2099-09", 12000, True, "振込"
    )
    assert saved["amount"] == 12000
    assert saved["paid"] is True


def test_carte_pages_include_home_card_and_teacher_note_editor(client):
    http, _, _ = client
    student_html = http.get("/kanto/carte").get_data(as_text=True)
    assert 'id="home"' in student_html
    assert "function renderHome(data)" in student_html
    import carte

    assert 'id="teacherNote"' in carte.ADMIN_HTML
    assert "teacher_note:teacherNote.value" in carte.ADMIN_HTML
