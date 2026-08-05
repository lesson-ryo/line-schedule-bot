"""生徒別レパートリーカルテ。

教材マスターは既存Googleスプレッドシートから読み取り、生徒別データだけを
既存のstorage.py（本番はUpstash Redis）へ保存する。
"""

from __future__ import annotations

import csv
import io
import os
import time
from datetime import datetime, timezone

import requests
from flask import Blueprint, abort, request

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
        row += [""] * (6 - len(row))
        material_id, kind, title, artist, video = [v.strip() for v in row[:5]]
        if not material_id.isdigit() or not title:
            continue
        items.append(
            {
                "id": int(material_id),
                "kind": kind,
                "title": title,
                "artist": artist,
                "video": video,
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

    allowed = {"status", "student_note"} if actor == "student" else {
        "status", "student_note", "teacher_note", "next_lesson"
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
        for k in ("status", "student_note", "teacher_note", "next_lesson")
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
*{box-sizing:border-box}body{margin:0;background:#f6f7f8;color:#202124;font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans",sans-serif}.head{position:sticky;top:0;z-index:2;background:#fff;padding:16px;border-bottom:1px solid #ddd}.head h1{font-size:20px;margin:0 0 12px}.search{width:100%;padding:12px;border:1px solid #ccc;border-radius:10px;font-size:16px}.tabs{display:flex;gap:6px;overflow:auto;padding-top:10px}.tabs button{white-space:nowrap;border:0;border-radius:18px;padding:8px 12px}.tabs .on{background:#06c755;color:#fff}.wrap{padding:12px;max-width:720px;margin:auto}.card{background:#fff;border:1px solid #e2e2e2;border-radius:12px;padding:14px;margin-bottom:10px}.title{font-weight:700}.sub{font-size:13px;color:#777;margin:4px 0 10px}.row{display:flex;gap:8px}.row select,.row button{flex:1;padding:10px;border-radius:8px;border:1px solid #ccc;background:#fff}.note{width:100%;margin-top:9px;padding:9px;border:1px solid #ddd;border-radius:8px}.next{color:#d26900;font-size:12px;font-weight:700}.empty{text-align:center;color:#888;padding:40px 10px}.video{font-size:13px;color:#087f5b}.save{background:#06c755!important;color:#fff;border:0!important}#msg{padding:10px;text-align:center;color:#666}
</style></head><body><div class="head"><h1 id="heading">マイカルテ</h1><input class="search" id="q" placeholder="曲名・アーティストを検索"><div class="tabs" id="tabs"></div></div><div id="msg">読み込み中…</div><main class="wrap" id="list"></main><script>
const LIFF_ID='__LIFF_ID__', labels={all:'すべて',next:'次回',practicing:'練習中',planned:'練習予定',completed:'完了',paused:'保留'};let token='',materials=[],progress={},filter='practicing';
const esc=s=>String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(url,body){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...body,idToken:token})});const d=await r.json();if(!r.ok)throw Error(d.error||'エラー');return d}
function drawTabs(){tabs.innerHTML=Object.entries(labels).map(([k,v])=>`<button class="${filter===k?'on':''}" onclick="filter='${k}';drawTabs();draw()">${v}</button>`).join('')}
function draw(){let q=document.getElementById('q').value.toLowerCase();let xs=materials.filter(m=>{let p=progress[m.id];if(filter==='next'&&!p?.next_lesson)return false;if(!['all','next'].includes(filter)&&p?.status!==filter)return false;return (m.title+' '+m.artist+' '+m.kind).toLowerCase().includes(q)});list.innerHTML=xs.length?xs.map(m=>{let p=progress[m.id]||{};return `<section class="card"><div class="${p.next_lesson?'next':''}">${p.next_lesson?'★ 次回レッスン':''}</div><div class="title">${esc(m.title)}</div><div class="sub">${esc([m.artist,m.kind].filter(Boolean).join(' ／ '))}</div>${m.video?`<a class="video" href="${esc(m.video.split(/\s/).find(x=>x.startsWith('http'))||'#')}">演奏動画を開く</a>`:''}<div class="row"><select id="s${m.id}">${Object.entries(labels).filter(x=>!['all','next'].includes(x[0])).map(([k,v])=>`<option value="${k}" ${(p.status||'planned')===k?'selected':''}>${v}</option>`).join('')}</select><button class="save" onclick="save(${m.id})">保存</button></div><textarea class="note" id="n${m.id}" rows="2" placeholder="自分用メモ">${esc(p.student_note)}</textarea>${p.teacher_note?`<div class="sub">講師メモ：${esc(p.teacher_note)}</div>`:''}</section>`}).join(''):'<div class="empty">該当する曲はありません</div>'}
async function save(id){try{let p=await api('/api/carte/progress',{material_id:id,status:document.getElementById('s'+id).value,student_note:document.getElementById('n'+id).value});progress[id]=p.progress;draw()}catch(e){alert(e.message)}}
async function main(){await liff.init({liffId:LIFF_ID});if(!liff.isLoggedIn()){liff.login();return}token=liff.getIDToken();let d=await api('/api/carte/me',{});materials=d.materials;progress=Object.fromEntries(d.progress.map(p=>[p.material_id,p]));heading.textContent=(d.display_name||'')+'さんのカルテ';msg.style.display='none';drawTabs();draw();q.oninput=draw}
main().catch(e=>msg.textContent=e.message);
</script></body></html>"""


ADMIN_HTML = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>生徒カルテ管理</title><style>
*{box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans",sans-serif;margin:auto;max-width:1100px;padding:20px;background:#f7f7f7;color:#222}h1{font-size:22px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:10px}.card,.detail{background:#fff;border:1px solid #ddd;border-radius:12px;padding:14px}.card{cursor:pointer}.name{font-weight:700}.stats{font-size:13px;color:#666;margin-top:8px}.detail{margin-top:16px}.tool{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}.tool input{flex:1;min-width:220px;padding:10px}.song{border-top:1px solid #eee;padding:12px 0}.song:first-child{border:0}.row{display:grid;grid-template-columns:1fr 140px 120px;gap:8px}.row input,.row select,.row button{padding:9px;border:1px solid #ccc;border-radius:7px}.row button{background:#06c755;color:#fff;border:0}.meta{font-size:12px;color:#777}.back{color:#06783b;cursor:pointer}.history{font-size:12px;color:#777;margin-top:4px}@media(max-width:650px){.row{grid-template-columns:1fr}.grid{grid-template-columns:1fr}}
</style></head><body><h1>生徒カルテ管理</h1><div id="summary"><p>読み込み中…</p></div><div id="detail"></div><script>
const token=new URLSearchParams(location.search).get('token')||'', labels={planned:'練習予定',practicing:'練習中',completed:'完了',paused:'保留'};let data;
const esc=s=>String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function getData(){let r=await fetch('/admin/carte/data?token='+encodeURIComponent(token));data=await r.json();if(!r.ok)throw Error(data.error||'取得失敗');renderSummary()}
function renderSummary(){detail.innerHTML='';summary.innerHTML=`<div class="tool"><input id="sq" placeholder="生徒名を検索" oninput="renderSummary()"><span>生徒 ${data.students.length}人／教材 ${data.materials.length}件</span></div><div class="grid">${data.students.filter(s=>!window.sq||s.display_name.includes(sq.value)).map(s=>`<div class="card" onclick="openStudent('${esc(s.user_id)}')"><div class="name">${esc(s.display_name)}</div><div class="stats">次回 ${s.next_count}曲　練習中 ${s.practicing_count}曲　完了 ${s.completed_count}曲</div><div class="meta">最終更新 ${esc(s.updated_at||'—')}</div></div>`).join('')}</div>`}
function openStudent(uid){let s=data.students.find(x=>x.user_id===uid), p=Object.fromEntries(data.progress.filter(x=>x.user_id===uid).map(x=>[x.material_id,x]));summary.innerHTML='<span class="back" onclick="renderSummary()">← 生徒一覧へ</span>';detail.innerHTML=`<section class="detail"><h2>${esc(s.display_name)}</h2><div class="tool"><input id="mq" placeholder="曲名を検索" oninput="drawSongs('${esc(uid)}')"><select id="mf" onchange="drawSongs('${esc(uid)}')"><option value="active">登録曲</option><option value="all">全教材</option><option value="practicing">練習中</option><option value="completed">完了</option></select></div><div id="songs"></div></section>`;window.currentP=p;drawSongs(uid)}
function drawSongs(uid){let q=(window.mq?.value||'').toLowerCase(),f=window.mf?.value||'active';let xs=data.materials.filter(m=>{let p=currentP[m.id];if(f==='active'&&!p)return false;if(!['active','all'].includes(f)&&p?.status!==f)return false;return(m.title+' '+m.artist).toLowerCase().includes(q)});songs.innerHTML=xs.slice(0,200).map(m=>{let p=currentP[m.id]||{};return `<div class="song"><b>${esc(m.title)}</b><div class="meta">${esc([m.artist,m.kind].filter(Boolean).join(' ／ '))}</div><div class="row"><select id="as${m.id}">${Object.entries(labels).map(([k,v])=>`<option value="${k}" ${(p.status||'planned')===k?'selected':''}>${v}</option>`).join('')}</select><label><input type="checkbox" id="an${m.id}" ${p.next_lesson?'checked':''}> 次回</label><button onclick="save('${esc(uid)}',${m.id})">保存</button><input id="ast${m.id}" value="${esc(p.student_note)}" placeholder="生徒メモ"><input id="at${m.id}" value="${esc(p.teacher_note)}" placeholder="講師メモ"></div></div>`}).join('')||'<p>該当する曲はありません</p>'}
async function save(uid,id){let body={user_id:uid,material_id:id,status:document.getElementById('as'+id).value,next_lesson:document.getElementById('an'+id).checked,student_note:document.getElementById('ast'+id).value,teacher_note:document.getElementById('at'+id).value};let r=await fetch('/admin/carte/progress?token='+encodeURIComponent(token),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});let d=await r.json();if(!r.ok)return alert(d.error||'保存失敗');currentP[id]=d.progress;await getData();openStudent(uid)}
getData().catch(e=>summary.innerHTML='<p>'+esc(e.message)+'</p>');
</script></body></html>"""


def create_carte_blueprint(verify_liff_user, upsert_member, admin_token, liff_id):
    bp = Blueprint("carte", __name__)

    def require_admin():
        if not admin_token or request.args.get("token", "") != admin_token:
            abort(403)

    @bp.get("/carte")
    @bp.get("/liff/carte")
    def student_page():
        return STUDENT_HTML.replace("__LIFF_ID__", liff_id)

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
        require_admin()
        return ADMIN_HTML

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
