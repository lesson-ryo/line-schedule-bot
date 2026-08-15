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
    separator = "&" if "?" in path else "?"
    return redirect(path + separator + urlencode({"notice": message}))


BASE_STYLE = """
*{box-sizing:border-box}body{margin:0;background:#f5f7f8;color:#202428;font-family:-apple-system,BlinkMacSystemFont,'Hiragino Sans',sans-serif}header{height:64px;background:#fff;border-bottom:1px solid #dfe3e6;display:flex;align-items:center;padding:0 22px;gap:14px}header h1{font-size:20px;margin:0}header a{color:#087f5b;text-decoration:none}header form{margin-left:auto}main{max-width:980px;margin:0 auto;padding:22px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:14px}.card{display:block;background:#fff;border:1px solid #dfe3e6;border-radius:11px;padding:18px;color:inherit;text-decoration:none}.card:hover{border-color:#75b9a2;box-shadow:0 5px 18px rgba(20,50,35,.07)}.card h2{font-size:17px;margin:0 0 7px}.card p,.muted{color:#687078;font-size:13px;line-height:1.55;margin:0}.tag{display:inline-block;font-size:10px;border-radius:10px;padding:3px 8px;background:#e8f4ef;color:#0f6e56;margin-bottom:9px}.button,button{display:inline-block;border:0;border-radius:7px;background:#087f5b;color:#fff;padding:10px 14px;font-size:14px;font-weight:700;text-decoration:none;cursor:pointer}.sub{background:#fff!important;color:#374047!important;border:1px solid #bec5c9!important}.notice{background:#e9f7f0;color:#0f6e56;border-radius:7px;padding:11px 13px;margin:0 0 15px}.error{background:#fff1f0;color:#a52b21;border-radius:7px;padding:11px 13px;margin:0 0 15px}label{display:block;font-size:13px;font-weight:700;margin:14px 0 6px}input,select,textarea{width:100%;border:1px solid #bec5c9;border-radius:7px;background:#fff;padding:10px 11px;font-size:16px}select,input{height:44px}textarea{min-height:88px;resize:vertical}.form{max-width:620px;background:#fff;border:1px solid #dfe3e6;border-radius:11px;padding:20px}.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}.actions{display:flex;gap:9px;margin-top:18px;flex-wrap:wrap}.status{width:100%;border-collapse:collapse;background:#fff;border:1px solid #dfe3e6}.status th,.status td{text-align:left;padding:11px 12px;border-bottom:1px solid #edf0f1;font-size:13px}.ok{color:#087f5b;font-weight:700}.bad{color:#b42318;font-weight:700}@media(max-width:620px){main{padding:15px}.row{grid-template-columns:1fr}header{padding:0 14px}}
"""

BASE_STYLE += """
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:0 0 18px}.stat{background:#fff;border:1px solid #dfe3e6;border-radius:10px;padding:14px}.stat b{display:block;font-size:24px;margin-bottom:3px}.stat span{font-size:12px;color:#687078}.section-title{font-size:16px;margin:24px 0 10px}.pill{display:inline-block;border-radius:12px;padding:3px 8px;font-size:10px;background:#eef1f2;color:#596168}.pill.on{background:#e8f4ef;color:#0f6e56}.pill.off{background:#fff1f0;color:#a52b21}.small-table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #dfe3e6}.small-table th,.small-table td{padding:10px;border-bottom:1px solid #edf0f1;text-align:left;font-size:12px;vertical-align:top}.small-table th{background:#f8faf9}.inline{display:inline}.danger{background:#fff!important;color:#a52b21!important;border:1px solid #dfb9b5!important}.warning{background:#fff8e6;color:#805400;border-radius:7px;padding:11px 13px;margin:0 0 15px}.target{background:#fff;border:1px solid #dfe3e6;border-radius:8px;padding:12px;margin:8px 0}.target b{font-size:13px}.target pre{white-space:pre-wrap;font-family:inherit;font-size:12px;color:#596168;margin:7px 0 0}.restore{border-top:1px solid #edf0f1;padding:12px 0}.restore:first-child{border-top:0}
"""

LOGIN_HTML = """<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>講師ログイン</title><style>{{style}}body{min-height:100vh;display:grid;place-items:center}.box{width:min(420px,calc(100% - 30px));background:#fff;border:1px solid #dfe3e6;border-radius:12px;padding:28px}.box h1{font-size:22px;margin:0 0 8px}</style></head><body><main class="box"><h1>講師ログイン</h1><p class="muted">日程調整・共通カルテ・曲リストを1つの画面から管理します。</p>{% if error %}<div class="error" style="margin-top:16px">{{error}}</div>{% endif %}<form method="post" action="/admin/login"><label for="password">管理用の合言葉</label><input id="password" name="password" type="password" autocomplete="current-password" autofocus required><button type="submit" style="width:100%;margin-top:16px">ログイン</button></form></main></body></html>"""

HOME_HTML = """<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>講師ホーム</title><style>{{style}}</style></head><body><header><h1>講師ホーム</h1><form method="post" action="/admin/logout"><button class="sub">ログアウト</button></form></header><main><div class="stats"><div class="stat"><b>{{dashboard.carte.students}}</b><span>カルテ生徒</span></div><div class="stat"><b>{{dashboard.carte.next_students}}</b><span>次回曲あり</span></div><div class="stat"><b>{{dashboard.carte.open_requests}}</b><span>未対応リクエスト</span></div><div class="stat"><b>{{dashboard.carte.completed}}</b><span>実施記録</span></div></div><div class="grid">{% for name,s in dashboard.schedules.items() %}<a class="card" href="/{{name}}/admin/panel"><span class="tag">{{s.label}}</span><h2>日程調整</h2><p>回答 {{s.responded}}/{{s.members}}人 ・ 未回答 {{s.nonrespondents}}人<br>候補 {{s.candidate_count}}件 ・ 確定 {{s.assignment_count}}枠</p></a><a class="card" href="/admin/reminders/{{name}}"><span class="tag">{{s.label}} LINE</span><h2>リマインド</h2><p>未回答者だけ、または明日の生徒だけに確認を送信</p></a>{% endfor %}<a class="card" href="/admin/automations"><span class="tag">自動LINE</span><h2>自動通知の設定</h2><p>未回答と前日確認を、設定時刻に自動送信</p></a><a class="card" href="/admin/lessons"><span class="tag">日程管理</span><h2>変更・キャンセル</h2><p>確定レッスンを変更し、生徒へのLINEとカレンダーへ反映</p></a><a class="card" href="/admin/attendance"><span class="tag">共通</span><h2>出欠・月謝</h2><p>月ごとの出席回数、欠席、月謝の入金状況を管理</p></a><a class="card" href="/kanto/admin/carte"><span class="tag">共通</span><h2>生徒カルテ</h2><p>実施状況、やりたい曲、次回曲、生徒メモ</p></a><a class="card" href="/kanto/admin/carte/next"><span class="tag">LINE</span><h2>次回レッスンまとめ</h2><p>生徒ごとの次回曲を確認してLINEに送信</p></a><a class="card" href="/admin/songs"><span class="tag">曲リスト</span><h2>曲の管理</h2><p>追加、編集、非公開化、リクエストから登録</p></a><a class="card" href="/admin/calendar"><span class="tag">自動同期</span><h2>Googleカレンダー</h2><p>関西・関東を別カレンダーへ登録し、変更・キャンセルも反映</p></a><a class="card" href="/admin/maintenance"><span class="tag">安全管理</span><h2>状態確認・バックアップ</h2><p>状態確認、保存、過去データへの復元</p></a></div>{% if dashboard.upcoming %}<h2 class="section-title">これからのレッスン</h2><table class="small-table"><tr><th>地域</th><th>日時</th><th>生徒</th></tr>{% for item in dashboard.upcoming %}<tr><td><span class="tag">{{item.label}}</span></td><td>{{item.day}} {{item.time}}〜{{item.end}}</td><td>{{item.name}}</td></tr>{% endfor %}</table>{% endif %}</main></body></html>"""

SONGS_HTML = """<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>曲の管理</title><style>{{style}}</style></head><body><header><h1>曲の管理</h1><a href="/admin">講師ホームに戻る</a></header><main>{% if notice %}<div class="notice">{{notice}}</div>{% endif %}{% if error %}<div class="error">{{error}}</div>{% endif %}{% if request_item %}<div class="warning">「{{request_item.title}}」のリクエスト内容を入力しました。登録すると自動で「追加済み」になります。</div>{% endif %}<form class="form" method="post" action="/admin/songs"><input type="hidden" name="material_id" value="{{values.get('material_id','')}}"><input type="hidden" name="request_id" value="{{values.get('request_id','')}}"><h2 style="font-size:17px;margin:0">{{'曲を編集' if values.get('material_id') else '新しい曲を追加'}}</h2><p class="muted" style="margin-top:7px">同じ曲名または動画URLがある場合は保存しません。非公開にしてもIDと過去カルテは残ります。</p><label for="title">曲名</label><input id="title" name="title" maxlength="120" value="{{values.get('title','')}}" required><div class="row"><div><label for="instrument">楽器</label><select id="instrument" name="instrument"><option value="">未設定</option>{% for v in ['ウクレレ','ギター'] %}<option value="{{v}}"{% if values.get('instrument')==v %} selected{% endif %}>{{v}}</option>{% endfor %}</select></div><div><label for="kind">形態</label><select id="kind" name="kind"><option value="">未設定</option>{% for v in ['弾き語り','ソロ弾き','メロ弾き','デュオ'] %}<option value="{{v}}"{% if values.get('kind')==v %} selected{% endif %}>{{v}}</option>{% endfor %}</select></div></div><label for="artist">アーティスト</label><input id="artist" name="artist" maxlength="120" value="{{values.get('artist','')}}"><label for="video">YouTube URL</label><input id="video" name="video" type="url" maxlength="500" value="{{values.get('video','')}}" placeholder="https://youtu.be/..."><div class="row"><div><label for="genre">ジャンル</label><input id="genre" name="genre" maxlength="80" value="{{values.get('genre','')}}"></div><div><label for="note">メモ</label><input id="note" name="note" maxlength="500" value="{{values.get('note','')}}"></div></div><div class="actions"><button type="submit">{{'変更を保存' if values.get('material_id') else '曲を追加する'}}</button>{% if values.get('material_id') %}<a class="button sub" href="/admin/songs">編集をやめる</a>{% endif %}<a class="button sub" href="/kanto/admin/carte">カルテを開く</a></div></form><h2 class="section-title">登録曲（{{materials|length}}曲）</h2><input id="songSearch" placeholder="曲名・アーティストで絞り込み" oninput="filterSongs()" style="max-width:420px;margin-bottom:10px"><table class="small-table" id="songTable"><tr><th>ID</th><th>曲</th><th>分類</th><th>公開</th><th>操作</th></tr>{% for item in materials %}<tr data-search="{{(item.title+' '+item.artist)|lower}}"><td>{{item.id}}</td><td><b>{{item.title}}</b>{% if item.artist %}<br><span class="muted">{{item.artist}}</span>{% endif %}</td><td>{{item.instrument}} {{item.kind}}{% if item.genre %}<br><span class="muted">{{item.genre}}</span>{% endif %}</td><td><span class="pill {{'on' if item.active else 'off'}}">{{'公開' if item.active else '非公開'}}</span></td><td><a class="button sub" href="/admin/songs?edit_id={{item.id}}">編集</a> <form class="inline" method="post" action="/admin/songs/visibility" onsubmit="return confirm('{{item.title}}を{{'非公開' if item.active else '再公開'}}にしますか？')"><input type="hidden" name="material_id" value="{{item.id}}"><input type="hidden" name="action" value="{{'archive' if item.active else 'publish'}}"><button class="{{'danger' if item.active else ''}}">{{'非公開' if item.active else '再公開'}}</button></form></td></tr>{% endfor %}</table><script>function filterSongs(){let q=songSearch.value.toLowerCase();songTable.querySelectorAll('tr[data-search]').forEach(r=>r.hidden=!r.dataset.search.includes(q))}</script></main></body></html>"""

MAINTENANCE_HTML = """<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>状態確認・バックアップ</title><style>{{style}}</style></head><body><header><h1>状態確認・バックアップ</h1><a href="/admin">講師ホームに戻る</a></header><main>{% if notice %}<div class="notice">{{notice}}</div>{% endif %}<table class="status"><tr><th>項目</th><th>状態</th><th>詳細</th></tr><tr><td>データ保存</td><td class="{{'ok' if status.storage.ok else 'bad'}}">{{'正常' if status.storage.ok else 'エラー'}}</td><td>{{status.storage.backend}} {{status.storage.error}}</td></tr><tr><td>曲リスト</td><td class="{{'ok' if status.sheet.ok else 'bad'}}">{{'正常' if status.sheet.ok else 'エラー'}}</td><td>{{status.sheet.count}}曲 {{status.sheet.error}}</td></tr>{% for name,t in status.tenants.items() %}<tr><td>{{'関西' if name=='kansai' else '関東'}} LINE</td><td class="{{'ok' if t.line_configured else 'bad'}}">{{'設定済み' if t.line_configured else '未設定'}}</td><td>日程: {{'ON' if t.schedule_enabled else 'OFF'}} / カルテ: {{'ON' if t.carte_enabled else 'OFF'}}</td></tr>{% endfor %}<tr><td>起動維持</td><td class="{{'ok' if status.keepalive.get('enabled') else 'bad'}}">{{'ON' if status.keepalive.get('enabled') else 'OFF'}}</td><td>{% if not status.keepalive.get('configured') %}cron-job.org未設定{% elif status.keepalive.get('error') %}{{status.keepalive.get('error')}}{% else %}自動停止: {{status.keepalive.get('expires_at') or 'なし'}}{% endif %}</td></tr><tr><td>バックアップ</td><td>{{status.backup.count}}世代</td><td>最新: {{status.backup.latest_at or 'まだありません'}}</td></tr></table><div class="actions"><form method="post" action="/admin/maintenance/backup"><button>今すぐバックアップ</button></form><a class="button sub" href="/admin/maintenance/backup.json">現在のデータをダウンロード</a>{% if status.keepalive.get('configured') %}<form method="post" action="/admin/maintenance/keepalive"><input type="hidden" name="action" value="on"><button class="sub">起動維持を常時ON</button></form><form method="post" action="/admin/maintenance/keepalive"><input type="hidden" name="action" value="off"><button class="sub">起動維持をOFF</button></form>{% endif %}</div><h2 class="section-title">保存済みバックアップから復元</h2><div class="warning">復元の直前に現在データを自動保存します。誤って復元しても、その自動保存から戻せます。</div><div class="card">{% if snapshots %}{% for item in snapshots|reverse %}<div class="restore"><b>{{item.created_at}}</b><p class="muted">関西 {{item.kansai_members}}人 / 関東 {{item.kanto_members}}人 / カルテ記録 {{item.carte_progress}}件</p><a class="button sub" href="/admin/maintenance/restore/{{item.index}}">内容を確認して復元</a></div>{% endfor %}{% else %}<p class="muted">バックアップはまだありません。</p>{% endif %}</div></main></body></html>"""

RESTORE_HTML = """<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>バックアップ復元</title><style>{{style}}</style></head><body><header><h1>バックアップ復元</h1><a href="/admin/maintenance">戻る</a></header><main><div class="warning"><b>{{snapshot.created_at}}</b> の状態へ戻します。現在の状態は実行直前に自動保存されます。</div><div class="card"><p>関西メンバー {{snapshot.kansai_members}}人<br>関東メンバー {{snapshot.kanto_members}}人<br>カルテ記録 {{snapshot.carte_progress}}件</p><form method="post"><button class="danger" onclick="return confirm('このバックアップへ復元しますか？')">この状態へ復元する</button> <a class="button sub" href="/admin/maintenance">キャンセル</a></form></div></main></body></html>"""

REMINDERS_HTML = """<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{label}} リマインド</title><style>{{style}}</style></head><body><header><h1>{{label}} リマインド</h1><a href="/admin">講師ホームに戻る</a></header><main>{% if notice %}<div class="notice">{{notice}}</div>{% endif %}<p class="muted">現在の対象者と本文です。自動通知は設定時刻に送られ、必要なときはここから手動送信もできます。</p>{% for preview in previews %}<section class="card" style="margin-top:14px"><h2>{{preview.label}}（{{preview.targets|length}}人）</h2>{% if preview.targets %}{% for target in preview.targets %}<div class="target"><b>{{target.display_name or '名前未登録'}}</b><pre>{{target.text}}</pre></div>{% endfor %}<form method="post" action="/admin/reminders/{{tenant}}/{{preview.kind}}" onsubmit="return confirm('{{preview.targets|length}}人に送信しますか？')"><button>{{preview.targets|length}}人に今すぐLINE送信</button></form>{% else %}<p class="muted">現在、送信対象はいません。</p>{% endif %}</section>{% endfor %}</main></body></html>"""

CALENDAR_HTML = """<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Googleカレンダー</title><style>{{style}}</style></head><body><header><h1>Googleカレンダー</h1><a href="/admin">講師ホームに戻る</a></header><main>{% if notice %}<div class="notice">{{notice}}</div>{% endif %}<p class="muted">確定した日程を地域別カレンダーへ同期します。日程を確定・変更したときは自動同期され、ここから再同期もできます。</p><div class="grid" style="margin-top:16px">{% for item in statuses %}<section class="card"><span class="tag">{{item.label}}</span><h2>{{item.calendar_name}}</h2>{% if not item.configured %}<div class="error">連携設定がまだありません。</div>{% else %}<p>今後の確定 {{item.event_count}}件<br>同期済み {{item.synced_count}}件 ・ 未反映 {{item.pending_count}}件</p>{% if item.last_synced_at %}<p class="muted" style="margin-top:8px">最終同期: {{item.last_synced_at[:16].replace('T',' ')}}</p>{% endif %}{% if item.last_error %}<div class="error" style="margin-top:10px">前回エラー: {{item.last_error}}</div>{% endif %}<form method="post" action="/admin/calendar/{{item.tenant}}" style="margin-top:14px" onsubmit="return confirm('{{item.label}}の日程をGoogleカレンダーへ同期しますか？')"><button>{{item.label}}を今すぐ同期</button></form>{% endif %}</section>{% endfor %}</div><div class="warning" style="margin-top:16px">このアプリが作成した予定だけを更新・削除します。カレンダーに手入力した予定には触れません。</div></main></body></html>"""

AUTOMATIONS_HTML = """<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>自動通知</title><style>{{style}}</style></head><body><header><h1>自動通知</h1><a href="/admin">講師ホームに戻る</a></header><main>{% if notice %}<div class="notice">{{notice}}</div>{% endif %}<p class="muted">起動維持の定期確認を利用して送信します。同じ対象・同じ回の通知は二重送信しません。</p><div class="grid" style="margin-top:16px">{% for item in statuses %}<section class="card"><span class="tag">{{item.label}}</span><h2>{{item.label}}の設定</h2><form method="post" action="/admin/automations/{{item.tenant}}"><label><input type="checkbox" name="unanswered_enabled" value="1"{% if item.settings.unanswered_enabled %} checked{% endif %} style="width:auto;height:auto"> 未回答者へ自動送信</label><label>送信時刻（回答期限の24時間前から）</label><input type="time" name="unanswered_time" value="{{item.settings.unanswered_time}}"><label><input type="checkbox" name="tomorrow_enabled" value="1"{% if item.settings.tomorrow_enabled %} checked{% endif %} style="width:auto;height:auto"> 前日確認を自動送信</label><label>送信時刻</label><input type="time" name="tomorrow_time" value="{{item.settings.tomorrow_time}}"><div class="actions"><button>設定を保存</button></div></form>{% if item.last_run %}<p class="muted" style="margin-top:14px">最終処理: {{(item.last_run.completed_at or item.last_run.started_at or '')[:16].replace('T',' ')}} / {{item.last_run.count or 0}}人{% if item.last_run.last_error %}<br>エラー: {{item.last_run.last_error}}{% endif %}</p>{% else %}<p class="muted" style="margin-top:14px">自動送信の履歴はまだありません。</p>{% endif %}</section>{% endfor %}</div></main></body></html>"""

LESSONS_HTML = """<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>レッスン変更・キャンセル</title><style>{{style}}</style></head><body><header><h1>レッスン変更・キャンセル</h1><a href="/admin">講師ホームに戻る</a></header><main>{% if notice %}<div class="notice">{{notice}}</div>{% endif %}<p class="muted">変更後はGoogleカレンダーへ自動反映します。「生徒へLINE通知」を外すと、LINEを送らずに保存できます。</p>{% if lessons %}{% for item in lessons %}<section class="card" style="margin-top:14px"><span class="tag">{{item.label}}</span><h2>{{item.name or '名前未登録'}}</h2><form method="post" action="/admin/lessons/update"><input type="hidden" name="tenant" value="{{item.tenant}}"><input type="hidden" name="lesson_id" value="{{item.lesson_id}}"><div class="row"><div><label>日付</label><input type="date" name="day" value="{{item.date_value}}" required></div><div><label>教室</label><input name="location" value="{{item.location}}" maxlength="120"></div></div><div class="row"><div><label>開始</label><input type="time" name="time" value="{{item.time}}" required></div><div><label>終了</label><input type="time" name="end" value="{{item.end}}" required></div></div><label><input type="checkbox" name="notify" value="1" checked style="width:auto;height:auto"> 生徒へ変更内容をLINE通知</label><div class="actions"><button>変更を保存</button></div></form><form method="post" action="/admin/lessons/cancel" style="margin-top:10px" onsubmit="return confirm('このレッスンをキャンセルしますか？')"><input type="hidden" name="tenant" value="{{item.tenant}}"><input type="hidden" name="lesson_id" value="{{item.lesson_id}}"><label><input type="checkbox" name="notify" value="1" checked style="width:auto;height:auto"> 生徒へキャンセルをLINE通知</label><button class="danger">この回をキャンセル</button></form></section>{% endfor %}{% else %}<div class="card" style="margin-top:16px"><p class="muted">今後の確定レッスンはありません。</p></div>{% endif %}</main></body></html>"""

ATTENDANCE_HTML = """<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>出欠・月謝</title><style>{{style}}</style></head><body><header><h1>出欠・月謝</h1><a href="/admin">講師ホームに戻る</a></header><main>{% if notice %}<div class="notice">{{notice}}</div>{% endif %}<form method="get" action="/admin/attendance" class="form"><label>表示する月</label><div class="row"><input type="month" name="month" value="{{data.month}}"><button>この月を表示</button></div></form><h2 class="section-title">月間まとめ</h2><div class="grid">{% for student in data.students %}<section class="card"><h2>{{student.display_name}}</h2><p>出席 {{student.counts.attended}}回 / 予定 {{student.counts.scheduled}}回 / 欠席 {{student.counts.absent}}回 / キャンセル {{student.counts.cancelled}}回</p><form method="post" action="/admin/attendance/tuition"><input type="hidden" name="month" value="{{data.month}}"><input type="hidden" name="user_id" value="{{student.user_id}}"><label>月謝</label><input type="number" name="amount" min="0" value="{{student.amount}}"><label><input type="checkbox" name="paid" value="1"{% if student.paid %} checked{% endif %} style="width:auto;height:auto"> 入金済み</label><label>月謝メモ</label><input name="note" value="{{student.note}}" maxlength="500"><div class="actions"><button>月謝を保存</button></div></form></section>{% endfor %}</div><h2 class="section-title">レッスンごとの出欠</h2>{% if data.rows %}<table class="small-table"><tr><th>日時</th><th>地域・生徒</th><th>出欠・メモ</th></tr>{% for row in data.rows %}<tr><td>{{row.date_value}}<br>{{row.time}}〜{{row.end}}</td><td><span class="tag">{{row.label}}</span><br><b>{{row.display_name}}</b>{% if row.location %}<br><span class="muted">{{row.location}}</span>{% endif %}</td><td><form method="post" action="/admin/attendance/record"><input type="hidden" name="month" value="{{data.month}}"><input type="hidden" name="record_id" value="{{row.id}}"><select name="status">{% for key,label in data.statuses.items() %}<option value="{{key}}"{% if row.status==key %} selected{% endif %}>{{label}}</option>{% endfor %}</select><input name="note" value="{{row.note}}" maxlength="500" placeholder="メモ（任意）" style="margin-top:6px"><button style="margin-top:6px">保存</button></form></td></tr>{% endfor %}</table>{% else %}<div class="card"><p class="muted">この月のレッスン記録はありません。</p></div>{% endif %}</main></body></html>"""


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
        from lesson_operations import dashboard_data

        return render_template_string(
            HOME_HTML, style=BASE_STYLE, dashboard=dashboard_data()
        )

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
        from carte import (
            add_material,
            get_request,
            load_materials,
            mark_request_added,
            update_material,
        )

        values = dict(request.form) if request.method == "POST" else {}
        error = ""
        notice = request.args.get("notice", "")
        request_item = None
        if request.method == "POST":
            material_id = str(values.get("material_id", "")).strip()
            if material_id.isdigit():
                item, error = update_material(int(material_id), values, "update")
                source = item.get("source", "") if item else ""
            else:
                item, source, error = add_material(values)
            if not error:
                request_id = str(values.get("request_id", "")).strip()
                if request_id:
                    mark_request_added(request_id, item["id"])
                label = "変更を保存" if material_id else (
                    "Google Sheetとカルテに追加" if source == "sheet" else "共通カルテに追加"
                )
                return _notice_redirect(
                    "/admin/songs", f"ID {item['id']}：{label}しました"
                )
        else:
            edit_id = str(request.args.get("edit_id", "")).strip()
            request_id = str(request.args.get("request_id", "")).strip()
            try:
                materials = load_materials(force=True, include_inactive=True)
            except TypeError:  # keeps simple test doubles and older adapters compatible
                materials = load_materials(force=True)
            if edit_id.isdigit():
                item = next((row for row in materials if row.get("id") == int(edit_id)), None)
                if item:
                    values = {key: item.get(key, "") for key in (
                        "title", "instrument", "kind", "artist", "video", "note", "genre"
                    )}
                    values["material_id"] = str(item["id"])
                else:
                    error = "編集する曲が見つかりません。"
            elif request_id:
                request_item = get_request(request_id)
                if request_item:
                    values = {
                        "title": request_item.get("title", ""),
                        "artist": request_item.get("artist", ""),
                        "instrument": request_item.get("instrument", ""),
                        "note": request_item.get("comment", ""),
                        "request_id": request_id,
                    }
                else:
                    error = "リクエストが見つかりません。"
        try:
            materials = load_materials(
                force=bool(request.method == "POST"), include_inactive=True
            )
        except TypeError:  # keeps simple test doubles and older adapters compatible
            materials = load_materials(force=bool(request.method == "POST"))
        materials = [
            {
                "id": row.get("id", ""),
                "title": row.get("title", ""),
                "artist": row.get("artist", ""),
                "instrument": row.get("instrument", ""),
                "kind": row.get("kind", ""),
                "genre": row.get("genre", ""),
                "active": row.get("active", True),
                **row,
            }
            for row in materials
            if isinstance(row, dict)
        ]
        return render_template_string(
            SONGS_HTML,
            style=BASE_STYLE,
            values=values,
            error=error,
            notice=notice,
            materials=materials,
            request_item=request_item,
            sheet_writer=bool(os.environ.get("REPERTOIRE_SHEET_WRITE_URL", "").strip()),
        ), (400 if error else 200)

    @bp.post("/admin/songs/visibility")
    @teacher_only
    def song_visibility():
        from carte import update_material

        try:
            material_id = int(request.form.get("material_id", ""))
        except (TypeError, ValueError):
            abort(400)
        action = str(request.form.get("action", ""))
        item, error = update_material(material_id, action=action)
        if error:
            return _notice_redirect("/admin/songs", error)
        verb = "非公開にしました" if action == "archive" else "再公開しました"
        return _notice_redirect("/admin/songs", f"ID {item['id']}：{verb}")

    @bp.get("/admin/maintenance")
    @teacher_only
    def maintenance_page():
        from maintenance import list_snapshots, system_status

        return render_template_string(
            MAINTENANCE_HTML,
            style=BASE_STYLE,
            status=system_status(),
            snapshots=list_snapshots(),
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

    @bp.route("/admin/maintenance/restore/<int:index>", methods=["GET", "POST"])
    @teacher_only
    def restore_backup(index):
        from maintenance import list_snapshots, restore_snapshot

        snapshot = next((row for row in list_snapshots() if row["index"] == index), None)
        if not snapshot:
            abort(404)
        if request.method == "POST":
            try:
                result = restore_snapshot(index)
            except ValueError as exc:
                return _notice_redirect("/admin/maintenance", str(exc))
            return _notice_redirect(
                "/admin/maintenance",
                f"{result['restored_at']} の状態へ復元しました。復元直前の状態も保存済みです。",
            )
        return render_template_string(
            RESTORE_HTML, style=BASE_STYLE, snapshot=snapshot
        )

    @bp.get("/admin/reminders/<tenant>")
    @teacher_only
    def reminders(tenant):
        from lesson_operations import TENANT_LABELS, reminder_preview

        if tenant not in TENANT_LABELS:
            abort(404)
        return render_template_string(
            REMINDERS_HTML,
            style=BASE_STYLE,
            tenant=tenant,
            label=TENANT_LABELS[tenant],
            previews=[
                reminder_preview(tenant, "unanswered"),
                reminder_preview(tenant, "tomorrow"),
            ],
            notice=request.args.get("notice", ""),
        )

    @bp.get("/admin/calendar")
    @teacher_only
    def calendar_page():
        from lesson_operations import TENANT_LABELS, calendar_sync_status

        return render_template_string(
            CALENDAR_HTML,
            style=BASE_STYLE,
            statuses=[calendar_sync_status(name) for name in TENANT_LABELS],
            notice=request.args.get("notice", ""),
        )

    @bp.post("/admin/calendar/<tenant>")
    @teacher_only
    def calendar_sync(tenant):
        from lesson_operations import TENANT_LABELS, sync_calendar_schedule

        if tenant not in TENANT_LABELS:
            abort(404)
        result = sync_calendar_schedule(tenant)
        if result["ok"]:
            message = (
                f"{TENANT_LABELS[tenant]}を同期しました。"
                f"追加 {result['created']}件・更新 {result['updated']}件・削除 {result['deleted']}件"
            )
        else:
            message = f"{TENANT_LABELS[tenant]}の同期に失敗しました: {result['error']}"
        return _notice_redirect("/admin/calendar", message)

    @bp.post("/admin/reminders/<tenant>/<kind>")
    @teacher_only
    def send_reminder(tenant, kind):
        from app import push_text_message
        from lesson_operations import TENANT_LABELS, send_reminders

        if tenant not in TENANT_LABELS or kind not in {"unanswered", "tomorrow"}:
            abort(404)
        result = send_reminders(tenant, kind, push_text_message)
        if not result["ok"]:
            message = f"{result['count']}人まで送信しました。{result['error']} 再送前にLINE履歴を確認してください。"
        else:
            message = f"{result['label']}を{result['count']}人へ送信しました。"
        return _notice_redirect(
            f"/admin/reminders/{tenant}",
            message,
        )

    @bp.get("/admin/automations")
    @teacher_only
    def automations_page():
        from lesson_operations import TENANT_NAMES, automation_status

        return render_template_string(
            AUTOMATIONS_HTML,
            style=BASE_STYLE,
            statuses=[automation_status(name) for name in TENANT_NAMES],
            notice=request.args.get("notice", ""),
        )

    @bp.post("/admin/automations/<tenant>")
    @teacher_only
    def automations_save(tenant):
        from lesson_operations import TENANT_LABELS, save_automation_settings

        if tenant not in TENANT_LABELS:
            abort(404)
        try:
            save_automation_settings(tenant, request.form)
            message = f"{TENANT_LABELS[tenant]}の自動通知設定を保存しました。"
        except ValueError as exc:
            message = str(exc)
        return _notice_redirect("/admin/automations", message)

    @bp.get("/admin/lessons")
    @teacher_only
    def lessons_page():
        from lesson_operations import lessons_data

        return render_template_string(
            LESSONS_HTML,
            style=BASE_STYLE,
            lessons=lessons_data(),
            notice=request.args.get("notice", ""),
        )

    @bp.post("/admin/lessons/update")
    @teacher_only
    def lesson_update():
        from app import push_text_message
        from lesson_operations import TENANT_LABELS, update_lesson

        tenant = str(request.form.get("tenant") or "")
        if tenant not in TENANT_LABELS:
            abort(404)
        try:
            result = update_lesson(
                tenant,
                str(request.form.get("lesson_id") or ""),
                request.form,
                push_text=push_text_message,
                notify=bool(request.form.get("notify")),
            )
            message = f"{TENANT_LABELS[tenant]}のレッスンを変更しました。"
            if request.form.get("notify"):
                message += f" LINEは{result['sent']}人へ送信しました。"
            if result.get("error"):
                message += f" LINE送信エラー: {result['error']}"
            if result["calendar"].get("configured") and not result["calendar"].get("ok"):
                message += " Googleカレンダーは未反映です。"
        except ValueError as exc:
            message = f"保存できませんでした: {exc}"
        return _notice_redirect("/admin/lessons", message)

    @bp.post("/admin/lessons/cancel")
    @teacher_only
    def lesson_cancel():
        from app import push_text_message
        from lesson_operations import TENANT_LABELS, cancel_lesson

        tenant = str(request.form.get("tenant") or "")
        if tenant not in TENANT_LABELS:
            abort(404)
        try:
            result = cancel_lesson(
                tenant,
                str(request.form.get("lesson_id") or ""),
                push_text=push_text_message,
                notify=bool(request.form.get("notify")),
            )
            message = f"{TENANT_LABELS[tenant]}のレッスンをキャンセルしました。"
            if request.form.get("notify"):
                message += f" LINEは{result['sent']}人へ送信しました。"
            if result.get("error"):
                message += f" LINE送信エラー: {result['error']}"
            if result["calendar"].get("configured") and not result["calendar"].get("ok"):
                message += " Googleカレンダーは未反映です。"
        except ValueError as exc:
            message = f"キャンセルできませんでした: {exc}"
        return _notice_redirect("/admin/lessons", message)

    @bp.get("/admin/attendance")
    @teacher_only
    def attendance_page():
        from lesson_operations import attendance_month_data

        return render_template_string(
            ATTENDANCE_HTML,
            style=BASE_STYLE,
            data=attendance_month_data(request.args.get("month", "")),
            notice=request.args.get("notice", ""),
        )

    @bp.post("/admin/attendance/record")
    @teacher_only
    def attendance_record():
        from lesson_operations import update_attendance_record

        month = str(request.form.get("month") or "")
        try:
            update_attendance_record(
                str(request.form.get("record_id") or ""),
                str(request.form.get("status") or ""),
                str(request.form.get("note") or ""),
            )
            message = "出欠を保存しました。"
        except ValueError as exc:
            message = str(exc)
        return _notice_redirect(f"/admin/attendance?month={month}", message)

    @bp.post("/admin/attendance/tuition")
    @teacher_only
    def attendance_tuition():
        from lesson_operations import save_tuition_record

        month = str(request.form.get("month") or "")
        try:
            save_tuition_record(
                str(request.form.get("user_id") or ""),
                month,
                request.form.get("amount", "0"),
                bool(request.form.get("paid")),
                str(request.form.get("note") or ""),
            )
            message = "月謝を保存しました。"
        except ValueError as exc:
            message = str(exc)
        return _notice_redirect(f"/admin/attendance?month={month}", message)

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
