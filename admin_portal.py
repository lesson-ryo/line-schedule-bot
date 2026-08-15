"""Shared teacher home, song entry, backups, and system status."""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from functools import wraps
from urllib.parse import urlencode

from flask import (
    Blueprint,
    abort,
    make_response,
    redirect,
    render_template_string,
    request,
)

from admin_auth import (
    clear_teacher_cookie,
    password_ok,
    set_teacher_cookie,
    teacher_session_ok,
)


_failed_logins = defaultdict(list)


def _notice_redirect(path: str, message: str):
    return redirect(path + "?" + urlencode({"notice": message}))


BASE_STYLE = """
*{box-sizing:border-box}body{margin:0;background:#f5f7f8;color:#202428;font-family:-apple-system,BlinkMacSystemFont,'Hiragino Sans',sans-serif}header{height:64px;background:#fff;border-bottom:1px solid #dfe3e6;display:flex;align-items:center;padding:0 22px;gap:14px}header h1{font-size:20px;margin:0}header a{color:#087f5b;text-decoration:none}header form{margin-left:auto}main{max-width:980px;margin:0 auto;padding:22px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:14px}.card{display:block;background:#fff;border:1px solid #dfe3e6;border-radius:11px;padding:18px;color:inherit;text-decoration:none}.card:hover{border-color:#75b9a2;box-shadow:0 5px 18px rgba(20,50,35,.07)}.card h2{font-size:17px;margin:0 0 7px}.card p,.muted{color:#687078;font-size:13px;line-height:1.55;margin:0}.tag{display:inline-block;font-size:10px;border-radius:10px;padding:3px 8px;background:#e8f4ef;color:#0f6e56;margin-bottom:9px}.button,button{display:inline-block;border:0;border-radius:7px;background:#087f5b;color:#fff;padding:10px 14px;font-size:14px;font-weight:700;text-decoration:none;cursor:pointer}.sub{background:#fff!important;color:#374047!important;border:1px solid #bec5c9!important}.notice{background:#e9f7f0;color:#0f6e56;border-radius:7px;padding:11px 13px;margin:0 0 15px}.error{background:#fff1f0;color:#a52b21;border-radius:7px;padding:11px 13px;margin:0 0 15px}label{display:block;font-size:13px;font-weight:700;margin:14px 0 6px}input,select,textarea{width:100%;border:1px solid #bec5c9;border-radius:7px;background:#fff;padding:10px 11px;font-size:16px}select,input{height:44px}textarea{min-height:88px;resize:vertical}.form{max-width:620px;background:#fff;border:1px solid #dfe3e6;border-radius:11px;padding:20px}.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}.actions{display:flex;gap:9px;margin-top:18px;flex-wrap:wrap}.status{width:100%;border-collapse:collapse;background:#fff;border:1px solid #dfe3e6}.status th,.status td{text-align:left;padding:11px 12px;border-bottom:1px solid #edf0f1;font-size:13px}.ok{color:#087f5b;font-weight:700}.bad{color:#b42318;font-weight:700}@media(max-width:620px){main{padding:15px}.row{grid-template-columns:1fr}header{padding:0 14px}}
"""

LOGIN_HTML = """<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>講師ログイン</title><style>{{style}}body{min-height:100vh;display:grid;place-items:center}.box{width:min(420px,calc(100% - 30px));background:#fff;border:1px solid #dfe3e6;border-radius:12px;padding:28px}.box h1{font-size:22px;margin:0 0 8px}</style></head><body><main class="box"><h1>講師ログイン</h1><p class="muted">日程調整・共通カルテ・曲リストを1つの画面から管理します。</p>{% if error %}<div class="error" style="margin-top:16px">{{error}}</div>{% endif %}<form method="post" action="/admin/login"><label for="password">管理用の合言葉</label><input id="password" name="password" type="password" autocomplete="current-password" autofocus required><button type="submit" style="width:100%;margin-top:16px">ログイン</button></form></main></body></html>"""

HOME_HTML = """<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>講師ホーム</title><style>{{style}}</style></head><body><header><h1>講師ホーム</h1><form method="post" action="/admin/logout"><button class="sub">ログアウト</button></form></header><main><div class="grid"><a class="card" href="/kansai/admin/panel"><span class="tag">関西</span><h2>日程調整</h2><p>候補作成、LINE送信、回答集計、時間割の確定</p></a><a class="card" href="/kanto/admin/panel"><span class="tag">関東</span><h2>日程調整</h2><p>関西とは別データで候補と回答を管理</p></a><a class="card" href="/kanto/admin/carte"><span class="tag">共通</span><h2>生徒カルテ</h2><p>実施状況、やりたい曲、次回曲、生徒メモ</p></a><a class="card" href="/kanto/admin/carte/next"><span class="tag">LINE</span><h2>次回レッスンまとめ</h2><p>生徒ごとの次回曲を確認してLINEに送信</p></a><a class="card" href="/admin/songs"><span class="tag">曲リスト</span><h2>曲を追加</h2><p>曲名とYouTube URLを入力し、重複を確認して追加</p></a><a class="card" href="/admin/maintenance"><span class="tag">安全管理</span><h2>状態確認・バックアップ</h2><p>Redis、スプレッドシート、LINE設定、起動維持を確認</p></a></div></main></body></html>"""

SONGS_HTML = """<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>曲を追加</title><style>{{style}}</style></head><body><header><h1>曲を追加</h1><a href="/admin">講師ホームに戻る</a></header><main>{% if notice %}<div class="notice">{{notice}}</div>{% endif %}{% if error %}<div class="error">{{error}}</div>{% endif %}<form class="form" method="post" action="/admin/songs"><p class="muted">同じ曲名または動画URLがある場合は追加しません。{% if not sheet_writer %}<br>現在は共通カルテへ直接保存します。Google Sheet書き込み設定後は同じ画面からシートへ追加されます。{% endif %}</p><label for="title">曲名</label><input id="title" name="title" maxlength="120" value="{{values.get('title','')}}" required><div class="row"><div><label for="instrument">楽器</label><select id="instrument" name="instrument"><option value="">未設定</option>{% for v in ['ウクレレ','ギター'] %}<option value="{{v}}"{% if values.get('instrument')==v %} selected{% endif %}>{{v}}</option>{% endfor %}</select></div><div><label for="kind">形態</label><select id="kind" name="kind"><option value="">未設定</option>{% for v in ['弾き語り','ソロ弾き','メロ弾き','デュオ'] %}<option value="{{v}}"{% if values.get('kind')==v %} selected{% endif %}>{{v}}</option>{% endfor %}</select></div></div><label for="artist">アーティスト</label><input id="artist" name="artist" maxlength="120" value="{{values.get('artist','')}}"><label for="video">YouTube URL</label><input id="video" name="video" type="url" maxlength="500" value="{{values.get('video','')}}" placeholder="https://youtu.be/..."><div class="row"><div><label for="genre">ジャンル</label><input id="genre" name="genre" maxlength="80" value="{{values.get('genre','')}}"></div><div><label for="note">メモ</label><input id="note" name="note" maxlength="500" value="{{values.get('note','')}}"></div></div><div class="actions"><button type="submit">曲を追加する</button><a class="button sub" href="/kanto/admin/carte">カルテを開く</a></div></form></main></body></html>"""

MAINTENANCE_HTML = """<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>状態確認・バックアップ</title><style>{{style}}</style></head><body><header><h1>状態確認・バックアップ</h1><a href="/admin">講師ホームに戻る</a></header><main>{% if notice %}<div class="notice">{{notice}}</div>{% endif %}<table class="status"><tr><th>項目</th><th>状態</th><th>詳細</th></tr><tr><td>データ保存</td><td class="{{'ok' if status.storage.ok else 'bad'}}">{{'正常' if status.storage.ok else 'エラー'}}</td><td>{{status.storage.backend}} {{status.storage.error}}</td></tr><tr><td>曲リスト</td><td class="{{'ok' if status.sheet.ok else 'bad'}}">{{'正常' if status.sheet.ok else 'エラー'}}</td><td>{{status.sheet.count}}曲 {{status.sheet.error}}</td></tr>{% for name,t in status.tenants.items() %}<tr><td>{{'関西' if name=='kansai' else '関東'}} LINE</td><td class="{{'ok' if t.line_configured else 'bad'}}">{{'設定済み' if t.line_configured else '未設定'}}</td><td>日程: {{'ON' if t.schedule_enabled else 'OFF'}} / カルテ: {{'ON' if t.carte_enabled else 'OFF'}}</td></tr>{% endfor %}<tr><td>起動維持</td><td class="{{'ok' if status.keepalive.get('enabled') else 'bad'}}">{{'ON' if status.keepalive.get('enabled') else 'OFF'}}</td><td>{% if not status.keepalive.get('configured') %}cron-job.org未設定{% elif status.keepalive.get('error') %}{{status.keepalive.get('error')}}{% else %}自動停止: {{status.keepalive.get('expires_at') or 'なし'}}{% endif %}</td></tr><tr><td>バックアップ</td><td>{{status.backup.count}}世代</td><td>最新: {{status.backup.latest_at or 'まだありません'}}</td></tr></table><div class="actions"><form method="post" action="/admin/maintenance/backup"><button>今すぐバックアップ</button></form><a class="button sub" href="/admin/maintenance/backup.json">現在のデータをダウンロード</a>{% if status.keepalive.get('configured') %}<form method="post" action="/admin/maintenance/keepalive"><input type="hidden" name="action" value="on"><button class="sub">起動維持を常時ON</button></form><form method="post" action="/admin/maintenance/keepalive"><input type="hidden" name="action" value="off"><button class="sub">起動維持をOFF</button></form>{% endif %}</div><p class="muted" style="margin-top:16px">バックアップはUpstash内に最大14世代保存します。ダウンロードしたJSONは別の場所にも保管できます。</p></main></body></html>"""


def create_admin_portal_blueprint():
    bp = Blueprint("teacher_admin", __name__)

    def require_teacher():
        if not teacher_session_ok():
            abort(403)

    def teacher_only(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            require_teacher()
            return func(*args, **kwargs)

        return wrapped

    @bp.get("/admin")
    def home():
        if not teacher_session_ok():
            return redirect("/admin/login")
        return render_template_string(HOME_HTML, style=BASE_STYLE)

    @bp.route("/admin/login", methods=["GET", "POST"])
    def login():
        if teacher_session_ok():
            return redirect("/admin")
        error = ""
        if request.method == "POST":
            key = request.remote_addr or "unknown"
            now = time.time()
            _failed_logins[key] = [at for at in _failed_logins[key] if now - at < 600]
            if len(_failed_logins[key]) >= 5:
                error = "試行回数が多いため、10分ほど待ってからお試しください。"
            elif password_ok(request.form.get("password", "")):
                _failed_logins.pop(key, None)
                return set_teacher_cookie(make_response(redirect("/admin")))
            else:
                _failed_logins[key].append(now)
                error = "合言葉が違います。"
        return render_template_string(LOGIN_HTML, style=BASE_STYLE, error=error), (401 if error else 200)

    @bp.post("/admin/logout")
    def logout():
        response = make_response(redirect("/admin/login"))
        return clear_teacher_cookie(response)

    @bp.route("/admin/songs", methods=["GET", "POST"])
    @teacher_only
    def songs():
        from carte import add_material

        values = dict(request.form) if request.method == "POST" else {}
        error = ""
        notice = request.args.get("notice", "")
        if request.method == "POST":
            item, source, error = add_material(values)
            if not error:
                label = "Google Sheetとカルテ" if source == "sheet" else "共通カルテ"
                return _notice_redirect(
                    "/admin/songs", f"{item['id']}:{label}に追加しました"
                )
        return render_template_string(
            SONGS_HTML,
            style=BASE_STYLE,
            values=values,
            error=error,
            notice=notice,
            sheet_writer=bool(os.environ.get("REPERTOIRE_SHEET_WRITE_URL", "").strip()),
        ), (400 if error else 200)

    @bp.get("/admin/maintenance")
    @teacher_only
    def maintenance_page():
        from maintenance import system_status

        return render_template_string(
            MAINTENANCE_HTML,
            style=BASE_STYLE,
            status=system_status(),
            notice=request.args.get("notice", ""),
        )

    @bp.post("/admin/maintenance/backup")
    @teacher_only
    def create_backup():
        from maintenance import save_snapshot

        snapshot = save_snapshot()
        return _notice_redirect(
            "/admin/maintenance",
            snapshot["created_at"] + ":バックアップを保存しました",
        )

    @bp.get("/admin/maintenance/backup.json")
    @teacher_only
    def download_backup():
        from maintenance import build_snapshot

        payload = json.dumps(build_snapshot(), ensure_ascii=False, indent=2)
        response = make_response(payload)
        response.mimetype = "application/json"
        response.headers["Content-Disposition"] = "attachment; filename=lesson-backup.json"
        response.headers["Cache-Control"] = "no-store"
        return response

    @bp.post("/admin/maintenance/keepalive")
    @teacher_only
    def keepalive_action():
        import keepalive

        action = request.form.get("action", "")
        if action == "on":
            message = keepalive.arm("") or "cron-job.orgが設定されていません。"
        elif action == "off":
            message = keepalive.disarm() or "cron-job.orgが設定されていません。"
        else:
            abort(400)
        return _notice_redirect("/admin/maintenance", message)

    return bp
