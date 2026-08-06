"""生徒別レパートリーカルテ。

教材マスターは既存Googleスプレッドシートから読み取り、生徒別データだけを
既存のstorage.py（本番はUpstash Redis）へ保存する。
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import os
import time
from datetime import datetime, timezone

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
VALID_STATUSES = {"planned", "practicing", "completed", "paused"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def load_materials(force=False):
    """シートを教材マスターへ変換。空の予約行は公開しない。"""
    if not force and _material_cache["items"] and time.time() - _material_cache["at"] < 300:
        return _material_cache["items"]
    res = requests.get(SHEET_CSV_URL, timeout=15)
    res.raise_for_status()
    rows = csv.reader(io.StringIO(res.content.decode("utf-8-sig")))
    next(rows, None)
    items = []
    for row in rows:
        # シート列: A=ID B=楽器 C=形態 D=曲名 E=アーティスト F=Youtube G=メモ
        row += [""] * (7 - len(row))
        material_id, instrument, kind, title, artist, video, note = [
            v.strip() for v in row[:7]
        ]
        if not material_id.isdigit() or not title:
            continue
        items.append(
            {
                "id": int(material_id),
                "instrument": instrument,
                "kind": kind,
                "title": title,
                "artist": artist,
                "video": video,
                "note": note,
            }
        )
    items.sort(key=lambda x: x["id"], reverse=True)
    _material_cache.update(at=time.time(), items=items)
    return items


def _progress():
    return load_json("carte:progress", default=[])


def _history():
    return load_json("carte:history", default=[])


def _student_rows(user_id):
    return [r for r in _progress() if r.get("user_id") == user_id]


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
<script src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script><style>
:root{--green:#087f5b;--line:#dfe3e6;--muted:#687078}*{box-sizing:border-box}body{margin:0;background:#f5f7f8;color:#202428;font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans",sans-serif}.head{position:sticky;top:0;z-index:5;background:#fff;padding:13px 12px;border-bottom:1px solid var(--line)}h1{font-size:19px;margin:0 0 10px}.tools{display:flex;gap:7px}.tools input{min-width:0;flex:1;height:41px;border:1px solid #bec5c9;border-radius:8px;padding:0 10px;font-size:15px}.tools select{width:110px;border:1px solid #bec5c9;border-radius:8px;background:#fff;padding:0 5px}.count{font-size:11px;color:var(--muted);margin-top:7px}.sheet{margin:10px;background:#fff;border:1px solid var(--line);border-radius:9px;overflow:hidden}table{width:100%;border-collapse:collapse;table-layout:fixed}th{background:#f8faf9;font-size:12px;text-align:left;padding:9px;border-bottom:1px solid var(--line)}th:last-child{text-align:center;width:58%}td{border-bottom:1px solid var(--line);padding:10px 9px}.song{min-width:0}.song b{display:block;font-size:14px;line-height:1.35}.song span{display:block;color:var(--muted);font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:3px}.cell{text-align:left;border-left:1px solid var(--line);cursor:pointer}.cell:active{background:#eaf7f0}.cell-content{display:flex;align-items:center;gap:8px;min-width:0}.status-block{flex:0 0 72px;text-align:center}.done{display:block;color:var(--green);font-weight:700;font-size:12px}.notdone{display:block;color:#8a9298;font-size:12px}.lesson-date{display:block;color:#596168;font-size:10px;margin-top:3px}.memo-preview{flex:1;min-width:0;color:#5f4a12;background:#fff8dc;border-radius:5px;padding:5px 6px;font-size:10px;line-height:1.35;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.empty{text-align:center;color:var(--muted);padding:50px 10px}dialog{border:0;border-radius:12px;padding:0;width:calc(100% - 28px);max-width:390px;box-shadow:0 18px 60px rgba(0,0,0,.25)}dialog::backdrop{background:rgba(20,30,26,.42)}.modal{padding:22px}.modal h2{font-size:18px;margin:0 0 18px}.choice{display:flex;gap:8px;margin-bottom:16px}.choice label{flex:1;border:1px solid var(--line);border-radius:8px;padding:12px 5px;text-align:center;font-size:13px}.field{margin-top:14px}.field label{display:block;font-size:13px;font-weight:700;margin-bottom:6px}.field input,.field textarea{width:100%;border:1px solid #bec5c9;border-radius:7px;padding:10px;font-size:16px}.field input{height:44px}.field textarea{resize:vertical;min-height:88px}.actions{display:flex;gap:8px;margin-top:20px}.actions button{flex:1;height:43px;border-radius:7px;border:1px solid #bec5c9;background:#fff}.actions .save{background:var(--green);border-color:var(--green);color:#fff;font-weight:700}.notice{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);background:#183d31;color:#fff;padding:9px 16px;border-radius:7px;display:none;white-space:nowrap}
</style></head><body><div class="head"><h1 id="heading">マイカルテ</h1><div class="tools"><input id="q" placeholder="曲名・アーティストを検索"><select id="filter"><option value="all">すべて</option><option value="done">実施済み</option><option value="notdone">未実施</option></select></div><div class="count" id="count">読み込み中…</div></div><main class="sheet" id="sheet"><div class="empty">読み込み中…</div></main><dialog id="editor"><form class="modal" onsubmit="save(event)"><h2 id="editSong"></h2><div class="choice"><label><input type="radio" name="done" value="false"> 未実施</label><label><input type="radio" name="done" value="true"> ✓ 実施済み</label></div><div class="field"><label for="lessonDate">授業日</label><input type="date" id="lessonDate"></div><div class="field"><label for="studentNote">自由メモ</label><textarea id="studentNote" maxlength="1000" placeholder="練習のポイントや気づいたことを自由に入力"></textarea></div><div class="actions"><button type="button" onclick="editor.close()">キャンセル</button><button class="save" id="saveButton">保存</button></div></form></dialog><div class="notice" id="notice">保存しました</div><script>
const LIFF_ID='__LIFF_ID__';let token='',materials=[],progress={},editingId=null;const esc=s=>String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const isDone=p=>p?.lesson_done===true||(!('lesson_done' in (p||{}))&&p?.status==='completed');const lessonDate=p=>p?.lesson_date||(p?.status==='completed'&&p?.completed_at?p.completed_at.slice(0,10):'');const jaDate=s=>s?s.replace(/^(\d{4})-(\d{2})-(\d{2})$/,'$1/$2/$3'):'';
async function api(url,body){let r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...body,idToken:token})}),d=await r.json();if(!r.ok)throw Error(d.error||'エラー');return d}
function draw(){let query=q.value.toLowerCase(),f=filter.value,xs=materials.filter(m=>{let done=isDone(progress[m.id]);return(m.title+' '+m.artist+' '+m.kind).toLowerCase().includes(query)&&(f==='all'||(f==='done'&&done)||(f==='notdone'&&!done))});count.textContent=xs.length+'曲';sheet.innerHTML=xs.length?`<table><thead><tr><th>曲名</th><th>授業・メモ</th></tr></thead><tbody>${xs.map(m=>{let p=progress[m.id],done=isDone(p),d=lessonDate(p);return `<tr><td class="song"><b>${esc(m.title)}</b><span>${esc([m.artist,m.kind].filter(Boolean).join(' ／ '))}</span></td><td class="cell" onclick="openEditor(${m.id})"><div class="cell-content"><div class="status-block"><span class="${done?'done':'notdone'}">${done?'✓ 実施済み':'未実施'}</span>${d?`<span class="lesson-date">${jaDate(d)}</span>`:''}</div>${p?.student_note?`<span class="memo-preview" title="${esc(p.student_note)}">${esc(p.student_note)}</span>`:''}</div></td></tr>`}).join('')}</tbody></table>`:'<div class="empty">該当する曲はありません</div>'}
function openEditor(id){let m=materials.find(x=>x.id===id),p=progress[id];editingId=id;editSong.textContent=m.title;document.querySelector(`input[name="done"][value="${isDone(p)}"]`).checked=true;lessonDate.value=lessonDate(p);studentNote.value=p?.student_note||'';editor.showModal()}
async function save(e){e.preventDefault();let done=document.querySelector('input[name="done"]:checked')?.value==='true',button=saveButton;button.disabled=true;button.textContent='保存中';try{let d=await api('/api/carte/progress',{material_id:editingId,lesson_done:done,lesson_date:lessonDate.value,student_note:studentNote.value,status:done?'completed':'planned'});progress[editingId]=d.progress;editor.close();draw();notice.style.display='block';setTimeout(()=>notice.style.display='none',1800)}catch(err){alert(err.message)}finally{button.disabled=false;button.textContent='保存'}}
async function main(){await liff.init({liffId:LIFF_ID});if(!liff.isLoggedIn()){liff.login();return}token=liff.getIDToken();let d=await api('/api/carte/me',{});materials=d.materials;progress=Object.fromEntries(d.progress.map(p=>[p.material_id,p]));heading.textContent=(d.display_name||'')+'さんのカルテ';draw();q.oninput=draw;filter.onchange=draw;editor.addEventListener('click',e=>{if(e.target===editor)editor.close()})}main().catch(e=>sheet.innerHTML='<div class="empty">'+esc(e.message)+'</div>');
</script></body></html>"""


ADMIN_HTML = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>生徒カルテ管理</title><style>
:root{--green:#087f5b;--line:#dfe3e6;--muted:#687078;--bg:#f5f7f8}*{box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans",sans-serif;margin:0;background:var(--bg);color:#202428}header{height:62px;background:#fff;border-bottom:1px solid var(--line);display:flex;align-items:center;padding:0 24px}header h1{font-size:20px;margin:0}.page{padding:20px 24px}.toolbar{display:flex;align-items:center;gap:10px;margin-bottom:12px}.toolbar input,.toolbar select{height:40px;border:1px solid #bec5c9;border-radius:7px;background:#fff;padding:0 12px;font-size:14px}.toolbar input{width:360px}.count{color:var(--muted);font-size:13px}.hint{margin-left:auto;color:var(--muted);font-size:12px}.sheet{background:#fff;border:1px solid var(--line);border-radius:9px;overflow:auto;height:calc(100vh - 135px)}table{border-collapse:separate;border-spacing:0;min-width:100%;white-space:nowrap}th,td{border-right:1px solid var(--line);border-bottom:1px solid var(--line)}th{height:50px;padding:8px 12px;background:#f8faf9;font-size:13px;text-align:center;position:sticky;top:0;z-index:2}.song-head{left:0;z-index:4;text-align:left;min-width:290px}.song{position:sticky;left:0;z-index:1;background:#fff;min-width:290px;max-width:290px;padding:9px 12px}.song b{display:block;overflow:hidden;text-overflow:ellipsis}.song span{display:block;color:var(--muted);font-size:11px;overflow:hidden;text-overflow:ellipsis;margin-top:3px}.student{min-width:240px;max-width:240px}.cell{min-width:240px;max-width:240px;height:58px;padding:5px 8px;cursor:pointer;background:#fff}.cell:hover{background:#eef8f3}.cell-content{display:flex;align-items:center;gap:9px;min-width:0}.status-block{flex:0 0 86px;text-align:center}.done{display:block;color:#087f5b;font-weight:700;font-size:13px}.notdone{display:block;color:#8a9298;font-size:13px}.lesson-date{display:block;color:#596168;font-size:11px;margin-top:3px}.memo-preview{flex:1;min-width:0;color:#5f4a12;background:#fff8dc;border-radius:5px;padding:5px 7px;font-size:11px;line-height:1.35;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.empty{padding:50px;text-align:center;color:var(--muted)}dialog{border:0;border-radius:12px;padding:0;box-shadow:0 18px 60px rgba(0,0,0,.25);width:min(420px,calc(100% - 30px))}dialog::backdrop{background:rgba(20,30,26,.4)}.modal{padding:24px}.modal h2{font-size:19px;margin:0 0 5px}.modal .who{color:var(--muted);font-size:13px;margin-bottom:20px}.choice{display:flex;gap:8px;margin-bottom:16px}.choice label{flex:1;border:1px solid var(--line);border-radius:8px;padding:12px;text-align:center;cursor:pointer}.field{margin-top:14px}.field label{display:block;font-size:13px;font-weight:700;margin-bottom:6px}.field input,.field textarea{width:100%;border:1px solid #bec5c9;border-radius:7px;padding:0 10px;font-size:16px}.field input{height:43px}.field textarea{height:96px;padding:10px;resize:vertical;font-family:inherit}.actions{display:flex;gap:8px;margin-top:20px}.actions button{flex:1;height:42px;border-radius:7px;border:1px solid #bec5c9;background:#fff;cursor:pointer}.actions .save{background:var(--green);border-color:var(--green);color:#fff;font-weight:700}.notice{position:fixed;right:24px;bottom:20px;background:#183d31;color:#fff;padding:10px 16px;border-radius:7px;display:none}@media(max-width:700px){.page{padding:12px}.toolbar{flex-wrap:wrap}.toolbar input{width:100%}.hint{margin-left:0}.song-head,.song{min-width:220px;max-width:220px}}
</style></head><body><header><h1>生徒カルテ管理</h1></header><main class="page"><div class="toolbar"><input id="q" placeholder="曲名・アーティストを検索"><select id="filter"><option value="all">すべての曲</option><option value="used">実施記録がある曲</option></select><span class="count" id="count">読み込み中…</span><span class="hint">セルをクリックして実施状況・授業日・メモを入力</span></div><div class="sheet" id="sheet"><div class="empty">読み込み中…</div></div></main><dialog id="editor"><form class="modal" onsubmit="save(event)"><h2 id="editSong"></h2><div class="who" id="editStudent"></div><div class="choice"><label><input type="radio" name="done" value="false"> 未実施</label><label><input type="radio" name="done" value="true"> ✓ 実施済み</label></div><div class="field"><label for="lessonDate">授業日</label><input type="date" id="lessonDate"></div><div class="field"><label for="studentNote">生徒メモ</label><textarea id="studentNote" maxlength="1000" placeholder="生徒が書いたメモを確認・編集できます"></textarea></div><div class="actions"><button type="button" onclick="editor.close()">キャンセル</button><button class="save" id="saveButton">保存</button></div></form></dialog><div class="notice" id="notice">保存しました</div><script>
let data,progress={},editing={};const esc=s=>String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const key=(uid,mid)=>uid+'|'+mid;const isDone=p=>p?.lesson_done===true||(!('lesson_done' in (p||{}))&&p?.status==='completed');const lessonDate=p=>p?.lesson_date||(p?.status==='completed'&&p?.completed_at?p.completed_at.slice(0,10):'');const jaDate=s=>s?s.replace(/^(\d{4})-(\d{2})-(\d{2})$/,'$1/$2/$3'):'';
async function load(){let r=await fetch('/admin/carte/data'),d=await r.json();if(!r.ok)throw Error(d.error||'取得失敗');data=d;progress=Object.fromEntries(d.progress.map(p=>[key(p.user_id,p.material_id),p]));draw()}
function draw(){let query=q.value.toLowerCase(),f=filter.value;let materials=data.materials.filter(m=>{let matches=(m.title+' '+m.artist+' '+m.kind).toLowerCase().includes(query);let used=f==='all'||data.students.some(s=>progress[key(s.user_id,m.id)]);return matches&&used});count.textContent=materials.length+'曲 × '+data.students.length+'人';sheet.innerHTML=data.students.length?`<table><thead><tr><th class="song-head">曲名</th>${data.students.map(s=>`<th class="student">${esc(s.display_name||'名前未登録')}</th>`).join('')}</tr></thead><tbody>${materials.map(m=>`<tr><td class="song"><b>${esc(m.title)}</b><span>${esc([m.artist,m.kind].filter(Boolean).join(' ／ '))}</span></td>${data.students.map(s=>cell(m,s)).join('')}</tr>`).join('')}</tbody></table>`:'<div class="empty">生徒がまだ登録されていません</div>'}
function cell(m,s){let p=progress[key(s.user_id,m.id)],done=isDone(p),d=lessonDate(p);return `<td class="cell" onclick="openEditor('${esc(s.user_id)}',${m.id})"><div class="cell-content"><div class="status-block"><span class="${done?'done':'notdone'}">${done?'✓ 実施済み':'未実施'}</span>${d?`<span class="lesson-date">${jaDate(d)}</span>`:''}</div>${p?.student_note?`<span class="memo-preview" title="${esc(p.student_note)}">${esc(p.student_note)}</span>`:''}</div></td>`}
function openEditor(uid,mid){let s=data.students.find(x=>x.user_id===uid),m=data.materials.find(x=>x.id===mid),p=progress[key(uid,mid)];editing={uid,mid};editSong.textContent=m.title;editStudent.textContent=(s.display_name||'名前未登録')+'さん';document.querySelector(`input[name="done"][value="${isDone(p)}"]`).checked=true;lessonDate.value=lessonDate(p);studentNote.value=p?.student_note||'';editor.showModal()}
async function save(e){e.preventDefault();let done=document.querySelector('input[name="done"]:checked')?.value==='true',button=saveButton;button.disabled=true;button.textContent='保存中';let body={user_id:editing.uid,material_id:editing.mid,lesson_done:done,lesson_date:lessonDate.value,student_note:studentNote.value,status:done?'completed':'planned'};try{let r=await fetch('/admin/carte/progress',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),d=await r.json();if(!r.ok)throw Error(d.error||'保存失敗');progress[key(editing.uid,editing.mid)]=d.progress;editor.close();draw();notice.style.display='block';setTimeout(()=>notice.style.display='none',1800)}catch(err){alert(err.message)}finally{button.disabled=false;button.textContent='保存'}}
q.oninput=draw;filter.onchange=draw;editor.addEventListener('click',e=>{if(e.target===editor)editor.close()});load().catch(e=>sheet.innerHTML='<div class="empty">'+esc(e.message)+'</div>');
</script></body></html>"""


ADMIN_LOGIN_HTML = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>講師ログイン</title><style>
*{box-sizing:border-box}body{margin:0;background:#f5f7f8;color:#202428;font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans",sans-serif;min-height:100vh;display:grid;place-items:center}.box{width:min(420px,calc(100% - 32px));background:#fff;border:1px solid #dfe3e6;border-radius:12px;padding:30px;box-shadow:0 8px 30px rgba(20,40,30,.08)}h1{font-size:22px;margin:0 0 8px}.sub{color:#687078;font-size:14px;margin:0 0 24px}label{display:block;font-size:13px;font-weight:700;margin-bottom:7px}input{width:100%;height:46px;border:1px solid #bcc3c7;border-radius:7px;padding:0 12px;font-size:17px}button{width:100%;height:46px;border:0;border-radius:7px;background:#087f5b;color:#fff;font-size:16px;font-weight:700;margin-top:16px;cursor:pointer}.error{background:#fff1f0;color:#a52b21;border-radius:6px;padding:10px 12px;font-size:13px;margin-bottom:16px}
</style></head><body><main class="box"><h1>講師用カルテ</h1><p class="sub">管理用の合言葉を入力してください。</p>__ERROR__<form method="post" action="/admin/carte/login"><label for="password">合言葉</label><input id="password" name="password" type="password" autocomplete="current-password" autofocus required><button type="submit">ログイン</button></form></main></body></html>"""


def render_student_page(liff_id):
    return STUDENT_HTML.replace("__LIFF_ID__", liff_id)


def create_carte_blueprint(verify_liff_user, upsert_member, admin_token, liff_id):
    bp = Blueprint("carte", __name__)

    def admin_cookie_value():
        return hmac.new(
            admin_token.encode("utf-8"), b"carte-admin-login", hashlib.sha256
        ).hexdigest()

    def is_admin():
        if not admin_token:
            return False
        supplied = request.args.get("token", "")
        cookie = request.cookies.get("carte_admin", "")
        return hmac.compare_digest(supplied, admin_token) or hmac.compare_digest(
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
        upsert_member(user_id, name)
        return {"display_name": name, "materials": load_materials(), "progress": _student_rows(user_id)}

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

    @bp.post("/admin/carte/login")
    def admin_login():
        password = request.form.get("password", "")
        if not admin_token or not hmac.compare_digest(password, admin_token):
            error = '<div class="error">合言葉が違います。もう一度お試しください。</div>'
            return ADMIN_LOGIN_HTML.replace("__ERROR__", error), 401
        response = make_response(redirect("/admin/carte"))
        response.set_cookie(
            "carte_admin", admin_cookie_value(), max_age=60 * 60 * 24 * 30,
            secure=True, httponly=True, samesite="Strict"
        )
        return response

    @bp.post("/admin/carte/logout")
    def admin_logout():
        response = make_response(redirect("/admin/carte"))
        response.delete_cookie("carte_admin", secure=True, httponly=True, samesite="Strict")
        return response

    @bp.get("/admin/carte/data")
    def admin_data():
        require_admin()
        members = load_json("members", default=[])
        rows = _progress()
        students = []
        for member in members:
            mine = [r for r in rows if r.get("user_id") == member.get("user_id")]
            students.append(
                {
                    **member,
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
        }

    @bp.post("/admin/carte/progress")
    def admin_progress():
        require_admin()
        body = request.get_json(silent=True) or {}
        user_id = str(body.get("user_id") or "")
        member = next((m for m in load_json("members", default=[]) if m.get("user_id") == user_id), None)
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

    @bp.post("/admin/carte/sync")
    def sync_materials():
        require_admin()
        return {"ok": True, "count": len(load_materials(force=True))}

    return bp
