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
# "wanted" = 生徒または講師が「やりたい」と付けた状態。未実施と実施済みの中間。
VALID_STATUSES = {"planned", "wanted", "practicing", "completed", "paused"}


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
        # シート列: A=ID B=楽器 C=形態 D=曲名 E=アーティスト F=Youtube G=メモ H=ジャンル
        # H列はまだシートに無くてもよい（無ければ空文字になる）。列を増やしたらここも直す。
        row += [""] * (8 - len(row))
        material_id, instrument, kind, title, artist, video, note, genre = [
            v.strip() for v in row[:8]
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
                "genre": genre,
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


def _prefs():
    """生徒ごとの設定。今は楽器だけ。{user_id: {"instrument": "ウクレレ"}}"""
    return load_json("carte:prefs", default={})


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
<script src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script><style>
:root{--green:#087f5b;--amber:#a06a00;--line:#dfe3e6;--muted:#687078}*{box-sizing:border-box}body{margin:0;background:#f5f7f8;color:#202428;font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans",sans-serif}.head{position:sticky;top:0;z-index:5;background:#fff;padding:13px 12px;border-bottom:1px solid var(--line)}h1{font-size:19px;margin:0 0 10px}.tools{display:flex;gap:7px}.tools input{min-width:0;flex:1;height:41px;border:1px solid #bec5c9;border-radius:8px;padding:0 10px;font-size:15px}.tools select{width:110px;border:1px solid #bec5c9;border-radius:8px;background:#fff;padding:0 5px}.tools .req{flex:0 0 auto;height:41px;border:1px solid var(--green);border-radius:8px;background:#fff;color:var(--green);font-size:13px;font-weight:700;padding:0 12px}.reqrow{display:flex;align-items:center;gap:8px;padding:10px 0;border-bottom:1px solid #eef0f1}.reqrow:last-child{border-bottom:0}.reqname{flex:1;min-width:0}.reqname b{display:block;font-size:14px}.reqname span{display:block;color:var(--muted);font-size:10px;margin-top:2px}.metoo{flex:0 0 auto;height:34px;border:1px solid #bec5c9;border-radius:17px;background:#fff;font-size:12px;padding:0 13px}.metoo.on{background:var(--green);border-color:var(--green);color:#fff;font-weight:700}.added{flex:0 0 auto;font-size:10px;color:var(--green);font-weight:700}.count{font-size:11px;color:var(--muted);margin-top:7px}.sheet{margin:10px;background:#fff;border:1px solid var(--line);border-radius:9px;overflow:hidden}table{width:100%;border-collapse:collapse;table-layout:fixed}th{background:#f8faf9;font-size:12px;text-align:left;padding:9px;border-bottom:1px solid var(--line)}th:last-child{text-align:center;width:58%}td{border-bottom:1px solid var(--line);padding:10px 9px}.song{min-width:0}.song .t{line-height:1.35}.song b{font-size:14px}.song em{font-style:normal;color:var(--muted);font-size:11px;margin-left:6px}.song span.meta{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:4px}.tag{display:inline-block;font-size:9px;padding:2px 7px;border-radius:9px;margin-right:3px}.tag.uk{background:#e6f1fb;color:#0c447c}.tag.gt{background:#faece7;color:#712b13}.tag.kind{background:#f1efe8;color:#444441}.tag.genre{background:#eeedfe;color:#3c3489}.tag.pop{background:#fdf0e6;color:#993c1d}.vid{display:inline-block;font-size:9px;padding:2px 8px;border-radius:9px;margin-right:3px;background:#fdeaea;color:#a32d2d;font-weight:700;text-decoration:none}.nextmark{display:inline-block;font-size:10px;padding:2px 7px;border-radius:10px;background:#fff3d6;color:#854f0b;margin-top:3px}.onlyins{font-size:11px;color:var(--muted);margin-top:6px}.cell{text-align:left;border-left:1px solid var(--line);cursor:pointer}.cell:active{background:#eaf7f0}.cell-content{display:flex;align-items:center;gap:8px;min-width:0}.status-block{flex:0 0 72px;text-align:center}.done{display:block;color:var(--green);font-weight:700;font-size:12px}.wanted{display:block;color:var(--amber);font-weight:700;font-size:12px}.notdone{display:block;color:#8a9298;font-size:12px}.lesson-date{display:block;color:#596168;font-size:10px;margin-top:3px}.memo-preview{flex:1;min-width:0;color:#5f4a12;background:#fff8dc;border-radius:5px;padding:5px 6px;font-size:10px;line-height:1.35;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.empty{text-align:center;color:var(--muted);padding:50px 10px}dialog{border:0;border-radius:12px;padding:0;width:calc(100% - 28px);max-width:390px;box-shadow:0 18px 60px rgba(0,0,0,.25)}dialog::backdrop{background:rgba(20,30,26,.42)}.modal{padding:22px}.modal h2{font-size:18px;margin:0 0 18px}.choice{display:flex;gap:6px;margin-bottom:16px}.choice label{flex:1;border:1px solid var(--line);border-radius:8px;padding:12px 3px;text-align:center;font-size:12px}.field{margin-top:14px}.field label{display:block;font-size:13px;font-weight:700;margin-bottom:6px}.field input,.field textarea{width:100%;border:1px solid #bec5c9;border-radius:7px;padding:10px;font-size:16px}.field input{height:44px}.field textarea{resize:vertical;min-height:88px}.actions{display:flex;gap:8px;margin-top:20px}.actions button{flex:1;height:43px;border-radius:7px;border:1px solid #bec5c9;background:#fff}.actions .save{background:var(--green);border-color:var(--green);color:#fff;font-weight:700}.notice{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);background:#183d31;color:#fff;padding:9px 16px;border-radius:7px;display:none;white-space:nowrap}
</style></head><body><div class="head"><h1 id="heading">マイカルテ</h1><div class="tools"><input id="q" placeholder="曲名・アーティストを検索"><select id="filter"><option value="all">すべて</option><option value="done">実施済み</option><option value="wanted">やりたい</option><option value="notdone">未実施</option><option value="next">次回レッスン</option><option value="popular">みんなのやりたい曲</option></select><button class="req" onclick="openRequests()">リクエスト</button></div><div class="count" id="count">読み込み中…</div><div class="onlyins" id="onlyIns"></div></div><main class="sheet" id="sheet"><div class="empty">読み込み中…</div></main><dialog id="editor"><form class="modal" onsubmit="save(event)"><h2 id="editSong"></h2><div class="choice"><label><input type="radio" name="state" value="notdone"> 未実施</label><label><input type="radio" name="state" value="wanted"> ★ やりたい</label><label><input type="radio" name="state" value="done"> ✓ 実施済み</label></div><div class="field"><label for="lessonDate">授業日</label><input type="date" id="lessonDate"></div><div class="field"><label for="studentNote">自由メモ</label><textarea id="studentNote" maxlength="1000" placeholder="練習のポイントや気づいたことを自由に入力"></textarea></div><div class="actions"><button type="button" onclick="editor.close()">キャンセル</button><button class="save" id="saveButton">保存</button></div></form></dialog><dialog id="reqDialog"><div class="modal"><h2>リクエスト</h2><p style="color:#687078;font-size:12px;margin:-12px 0 16px">リストに無い曲をリクエストできます。名前は他の生徒には出ません。</p><form onsubmit="sendRequest(event)"><div class="field"><label for="reqTitle">曲名</label><input id="reqTitle" maxlength="120" placeholder="必須" required></div><div class="field"><label for="reqArtist">アーティスト</label><input id="reqArtist" maxlength="120" placeholder="わかれば"></div><div class="field"><label for="reqInstrument">楽器</label><select id="reqInstrument" style="width:100%;height:44px;border:1px solid #bec5c9;border-radius:7px;padding:0 10px;font-size:16px;background:#fff"><option value="">どちらでも</option><option value="ウクレレ">ウクレレ</option><option value="ギター">ギター</option></select></div><div class="field"><label for="reqComment">ひとこと</label><textarea id="reqComment" maxlength="300" placeholder="なぜやりたいか、どのバージョンかなど"></textarea></div><div class="actions"><button type="button" onclick="reqDialog.close()">閉じる</button><button class="save" id="reqButton">送信</button></div></form><div style="margin-top:22px"><h2 style="font-size:15px;margin:0 0 4px">みんなのリクエスト</h2><p style="color:#687078;font-size:11px;margin:0 0 8px">同じ曲をやりたければ「私も」を押してください</p><div id="reqList"></div></div></div></dialog><div class="notice" id="notice">保存しました</div><script>
const LIFF_ID='__LIFF_ID__';let token='',materials=[],progress={},popular={},requests=[],editingId=null;const esc=s=>String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const isDone=p=>p?.lesson_done===true||(!('lesson_done' in (p||{}))&&p?.status==='completed');const stateOf=p=>isDone(p)?'done':(p?.status==='wanted'?'wanted':'notdone');const stateLabel=s=>s==='done'?'✓ 実施済み':(s==='wanted'?'★ やりたい':'未実施');const lessonDate=p=>p?.lesson_date||(p?.status==='completed'&&p?.completed_at?p.completed_at.slice(0,10):'');const jaDate=s=>s?s.replace(/^(\d{4})-(\d{2})-(\d{2})$/,'$1/$2/$3'):'';
async function api(url,body){let r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...body,idToken:token})}),d=await r.json();if(!r.ok)throw Error(d.error||'エラー');return d}
function tags(m){let out='';if(m.instrument)out+=`<span class="tag ${m.instrument==='ウクレレ'?'uk':'gt'}">${esc(m.instrument)}</span>`;if(m.kind)out+=`<span class="tag kind">${esc(m.kind)}</span>`;if(m.genre)out+=`<span class="tag genre">${esc(m.genre)}</span>`;let w=popular[m.id]?.wanted||0;if(w)out+=`<span class="tag pop">★${w}人</span>`;return out}
const videoUrls=s=>String(s||'').match(/https?:\/\/[^\s]+/g)||[];
function videos(m){let us=videoUrls(m.video);return us.map((u,i)=>`<a class="vid" href="${esc(u)}" onclick="playVideo(event,'${esc(u)}')">▶ ${us.length>1?'動画'+(i+1):'動画'}</a>`).join('')}
function playVideo(e,url){e.preventDefault();e.stopPropagation();if(window.liff&&liff.openWindow){liff.openWindow({url:url,external:true})}else{window.open(url,'_blank','noopener')}}
function draw(){let query=q.value.toLowerCase(),f=filter.value;let xs=materials.filter(m=>{let st=stateOf(progress[m.id]);if(!(m.title+' '+m.artist+' '+m.kind+' '+m.instrument+' '+(m.genre||'')).toLowerCase().includes(query))return false;if(f==='all')return true;if(f==='next')return !!progress[m.id]?.next_lesson;if(f==='popular')return (popular[m.id]?.wanted||0)>0;return f===st});
 if(f==='popular')xs=xs.slice().sort((a,b)=>(popular[b.id]?.wanted||0)-(popular[a.id]?.wanted||0));
 count.textContent=xs.length+'曲';sheet.innerHTML=xs.length?`<table><thead><tr><th>曲名</th><th>授業・メモ</th></tr></thead><tbody>${xs.map(m=>{let p=progress[m.id],st=stateOf(p),d=lessonDate(p);return `<tr><td class="song"><div class="t"><b>${esc(m.title)}</b>${m.artist?`<em>${esc(m.artist)}</em>`:''}</div><span class="meta">${tags(m)}${videos(m)}</span></td><td class="cell" onclick="openEditor(${m.id})"><div class="cell-content"><div class="status-block"><span class="${st}">${stateLabel(st)}</span>${d?`<span class="lesson-date">${jaDate(d)}</span>`:''}${p?.next_lesson?'<span class="nextmark">▶ 次回</span>':''}</div>${p?.student_note?`<span class="memo-preview" title="${esc(p.student_note)}">${esc(p.student_note)}</span>`:''}</div></td></tr>`}).join('')}</tbody></table>`:'<div class="empty">該当する曲はありません</div>'}
function drawRequests(){reqList.innerHTML=requests.length?requests.map(r=>`<div class="reqrow"><span class="reqname"><b>${esc(r.title)}</b><span>${esc([r.artist,r.instrument].filter(Boolean).join(' ／ '))||'&nbsp;'}</span></span>${r.status==='added'?'<span class="added">リストに追加済み</span>':`<button class="metoo${r.voted?' on':''}" onclick="vote('${esc(r.id)}')">${r.voted?'私も ✓':'私も'} ${r.votes}</button>`}</div>`).join(''):'<p class="empty" style="padding:20px 0">まだリクエストはありません</p>'}
function openRequests(){drawRequests();reqDialog.showModal()}
async function vote(id){try{let d=await api('/api/carte/request/vote',{id});requests=d.requests;drawRequests()}catch(e){alert(e.message)}}
async function sendRequest(e){e.preventDefault();let b=reqButton;b.disabled=true;b.textContent='送信中';try{let d=await api('/api/carte/request',{title:reqTitle.value,artist:reqArtist.value,instrument:reqInstrument.value,comment:reqComment.value});requests=d.requests;reqTitle.value='';reqArtist.value='';reqComment.value='';drawRequests();notice.textContent='リクエストを送りました';notice.style.display='block';setTimeout(()=>notice.style.display='none',2000)}catch(err){alert(err.message)}finally{b.disabled=false;b.textContent='送信'}}
function openEditor(id){let m=materials.find(x=>x.id===id),p=progress[id];editingId=id;editSong.textContent=m.title;document.querySelector(`input[name="state"][value="${stateOf(p)}"]`).checked=true;lessonDate.value=lessonDate(p);studentNote.value=p?.student_note||'';editor.showModal()}
async function save(e){e.preventDefault();let st=document.querySelector('input[name="state"]:checked')?.value||'notdone',done=st==='done',button=saveButton;button.disabled=true;button.textContent='保存中';try{let d=await api('/api/carte/progress',{material_id:editingId,lesson_done:done,lesson_date:lessonDate.value,student_note:studentNote.value,status:done?'completed':(st==='wanted'?'wanted':'planned')});progress[editingId]=d.progress;editor.close();draw();notice.style.display='block';setTimeout(()=>notice.style.display='none',1800)}catch(err){alert(err.message)}finally{button.disabled=false;button.textContent='保存'}}
async function main(){await liff.init({liffId:LIFF_ID});if(!liff.isLoggedIn()){liff.login();return}token=liff.getIDToken();let d=await api('/api/carte/me',{});materials=d.materials;popular=d.popular||{};requests=d.requests||[];progress=Object.fromEntries(d.progress.map(p=>[p.material_id,p]));heading.textContent=(d.display_name||'')+'さんのカルテ';onlyIns.textContent=d.instrument?d.instrument+'の曲だけを表示しています':'';draw();q.oninput=draw;filter.onchange=draw;editor.addEventListener('click',e=>{if(e.target===editor)editor.close()});reqDialog.addEventListener('click',e=>{if(e.target===reqDialog)reqDialog.close()})}main().catch(e=>sheet.innerHTML='<div class="empty">'+esc(e.message)+'</div>');
</script></body></html>"""


ADMIN_HTML = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>生徒カルテ管理</title><style>
:root{--green:#087f5b;--amber:#a06a00;--line:#dfe3e6;--muted:#687078;--bg:#f5f7f8}*{box-sizing:border-box}body{font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans",sans-serif;margin:0;background:var(--bg);color:#202428}header{height:62px;background:#fff;border-bottom:1px solid var(--line);display:flex;align-items:center;padding:0 24px}header h1{font-size:20px;margin:0}.page{padding:20px 24px}.toolbar{display:flex;align-items:center;gap:10px;margin-bottom:12px}.toolbar input,.toolbar select{height:40px;border:1px solid #bec5c9;border-radius:7px;background:#fff;padding:0 12px;font-size:14px}.toolbar input{width:360px}.toolbar button{height:40px;border:1px solid #bec5c9;border-radius:7px;background:#fff;padding:0 14px;font-size:13px;cursor:pointer}.toolbar button:hover{background:#f3f6f5}.toolbar button:disabled{color:#9aa1a6;cursor:default}.count{color:var(--muted);font-size:13px;white-space:nowrap}.link{color:#0f6e56;font-size:13px;text-decoration:none;border-bottom:1px solid #0f6e56;white-space:nowrap}.hint{margin-left:auto;color:var(--muted);font-size:12px}@media(max-width:1100px){.hint{display:none}}.sheet{background:#fff;border:1px solid var(--line);border-radius:9px;overflow:auto;height:calc(100vh - 135px)}table{border-collapse:separate;border-spacing:0;min-width:100%;white-space:nowrap}th,td{border-right:1px solid var(--line);border-bottom:1px solid var(--line)}th{height:50px;padding:8px 12px;background:#f8faf9;font-size:13px;text-align:center;position:sticky;top:0;z-index:2}.song-head{left:0;z-index:4;text-align:left;min-width:290px}.song{position:sticky;left:0;z-index:1;background:#fff;min-width:290px;max-width:290px;padding:9px 12px}.song .t{overflow:hidden;text-overflow:ellipsis}.song em{font-style:normal;color:var(--muted);font-size:12px;margin-left:7px}.song>span{display:block;overflow:hidden;text-overflow:ellipsis;margin-top:4px}.tag{display:inline-block;font-size:10px;padding:2px 8px;border-radius:10px;margin-right:4px;white-space:nowrap}.tag.uk{background:#e6f1fb;color:#0c447c}.tag.gt{background:#faece7;color:#712b13}.tag.kind{background:#f1efe8;color:#444441}.tag.genre{background:#eeedfe;color:#3c3489}.vid{display:inline-block;font-size:10px;padding:2px 8px;border-radius:10px;margin-right:4px;background:#fdeaea;color:#a32d2d;font-weight:700;text-decoration:none}.vid:hover{background:#f7d4d4}.student{min-width:240px;max-width:240px}.cell{min-width:240px;max-width:240px;height:58px;padding:5px 8px;cursor:pointer;background:#fff}.cell:hover{background:#eef8f3}.cell-content{display:flex;align-items:center;gap:9px;min-width:0}.status-block{flex:0 0 86px;text-align:center}.done{display:block;color:#087f5b;font-weight:700;font-size:13px}.wanted{display:block;color:var(--amber);font-weight:700;font-size:13px}.notdone{display:block;color:#8a9298;font-size:13px}.lesson-date{display:block;color:#596168;font-size:11px;margin-top:3px}.memo-preview{flex:1;min-width:0;color:#5f4a12;background:#fff8dc;border-radius:5px;padding:5px 7px;font-size:11px;line-height:1.35;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.empty{padding:50px;text-align:center;color:var(--muted)}dialog{border:0;border-radius:12px;padding:0;box-shadow:0 18px 60px rgba(0,0,0,.25);width:min(420px,calc(100% - 30px))}dialog::backdrop{background:rgba(20,30,26,.4)}.modal{padding:24px}.modal h2{font-size:19px;margin:0 0 5px}.modal .who{color:var(--muted);font-size:13px;margin-bottom:20px}.choice{display:flex;gap:6px;margin-bottom:16px}.choice label{flex:1;border:1px solid var(--line);border-radius:8px;padding:12px 4px;text-align:center;cursor:pointer;font-size:13px}.nextbox{display:block;border:1px solid #e0d3b0;background:#fffbf0;border-radius:8px;padding:11px 12px;font-size:13px;cursor:pointer}.student-set{display:block;margin-top:5px}.student-set select{height:26px;border:1px solid #cfd5d8;border-radius:5px;background:#fff;font-size:11px;padding:0 4px}.nextmark{display:inline-block;font-size:10px;padding:2px 7px;border-radius:10px;background:#fff3d6;color:#854f0b;margin-top:3px}.field{margin-top:14px}.field label{display:block;font-size:13px;font-weight:700;margin-bottom:6px}.field input,.field textarea{width:100%;border:1px solid #bec5c9;border-radius:7px;padding:0 10px;font-size:16px}.field input{height:43px}.field textarea{height:96px;padding:10px;resize:vertical;font-family:inherit}.actions{display:flex;gap:8px;margin-top:20px}.actions button{flex:1;height:42px;border-radius:7px;border:1px solid #bec5c9;background:#fff;cursor:pointer}.actions .save{background:var(--green);border-color:var(--green);color:#fff;font-weight:700}.notice{position:fixed;right:24px;bottom:20px;background:#183d31;color:#fff;padding:10px 16px;border-radius:7px;display:none}@media(max-width:700px){.page{padding:12px}.toolbar{flex-wrap:wrap}.toolbar input{width:100%}.hint{margin-left:0}.song-head,.song{min-width:220px;max-width:220px}}
</style></head><body><header><h1>生徒カルテ管理</h1></header><main class="page"><div class="toolbar"><input id="q" placeholder="曲名・アーティストを検索"><select id="filter"><option value="all">すべての曲</option><option value="used">実施記録がある曲</option><option value="wanted">やりたい人がいる曲</option><option value="next">次回レッスン曲</option></select><span class="count" id="count">読み込み中…</span><button id="syncBtn" onclick="sync()">シートを再読み込み</button><a class="link" href="/admin/carte/ranking">ランキング</a><a class="link" href="/admin/carte/requests">リクエスト曲</a><a class="link" href="/admin/carte/history">更新履歴</a><span class="hint">セルをクリックして実施状況・授業日・メモを入力</span></div><div class="sheet" id="sheet"><div class="empty">読み込み中…</div></div></main><dialog id="editor"><form class="modal" onsubmit="save(event)"><h2 id="editSong"></h2><div class="who" id="editStudent"></div><div class="choice"><label><input type="radio" name="state" value="notdone"> 未実施</label><label><input type="radio" name="state" value="wanted"> ★ やりたい</label><label><input type="radio" name="state" value="done"> ✓ 実施済み</label></div><label class="nextbox"><input type="checkbox" id="nextLesson"> ▶ 次回レッスンでやる</label><div class="field"><label for="lessonDate">授業日</label><input type="date" id="lessonDate"></div><div class="field"><label for="studentNote">生徒メモ</label><textarea id="studentNote" maxlength="1000" placeholder="生徒が書いたメモを確認・編集できます"></textarea></div><div class="actions"><button type="button" onclick="editor.close()">キャンセル</button><button class="save" id="saveButton">保存</button></div></form></dialog><div class="notice" id="notice">保存しました</div><script>
let data,progress={},editing={};const esc=s=>String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const key=(uid,mid)=>uid+'|'+mid;const isDone=p=>p?.lesson_done===true||(!('lesson_done' in (p||{}))&&p?.status==='completed');const stateOf=p=>isDone(p)?'done':(p?.status==='wanted'?'wanted':'notdone');const stateLabel=s=>s==='done'?'✓ 実施済み':(s==='wanted'?'★ やりたい':'未実施');const lessonDate=p=>p?.lesson_date||(p?.status==='completed'&&p?.completed_at?p.completed_at.slice(0,10):'');const jaDate=s=>s?s.replace(/^(\d{4})-(\d{2})-(\d{2})$/,'$1/$2/$3'):'';
async function load(){let r=await fetch('/admin/carte/data'),d=await r.json();if(!r.ok)throw Error(d.error||'取得失敗');data=d;progress=Object.fromEntries(d.progress.map(p=>[key(p.user_id,p.material_id),p]));draw()}
async function sync(){let b=syncBtn;b.disabled=true;b.textContent='読み込み中…';try{let r=await fetch('/admin/carte/sync',{method:'POST'}),d=await r.json();if(!r.ok)throw Error(d.error||'シートの読み込みに失敗しました');await load();notice.textContent=d.count+'曲を読み込みました';notice.style.display='block';setTimeout(()=>notice.style.display='none',2200)}catch(e){alert(e.message)}finally{b.disabled=false;b.textContent='シートを再読み込み'}}
function tags(m){let out='';if(m.instrument)out+=`<span class="tag ${m.instrument==='ウクレレ'?'uk':'gt'}">${esc(m.instrument)}</span>`;if(m.kind)out+=`<span class="tag kind">${esc(m.kind)}</span>`;if(m.genre)out+=`<span class="tag genre">${esc(m.genre)}</span>`;return out||'<span class="tag kind">形態未設定</span>'}
const videoUrls=s=>String(s||'').match(/https?:\/\/[^\s]+/g)||[];
function videos(m){let us=videoUrls(m.video);return us.map((u,i)=>`<a class="vid" href="${esc(u)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">▶ ${us.length>1?'動画'+(i+1):'動画'}</a>`).join('')}
function draw(){let query=q.value.toLowerCase(),f=filter.value;let materials=data.materials.filter(m=>{let matches=(m.title+' '+m.artist+' '+m.kind+' '+m.instrument+' '+(m.genre||'')).toLowerCase().includes(query);let keep=f==='all'||(f==='used'&&data.students.some(s=>progress[key(s.user_id,m.id)]))||(f==='wanted'&&data.students.some(s=>stateOf(progress[key(s.user_id,m.id)])==='wanted'))||(f==='next'&&data.students.some(s=>progress[key(s.user_id,m.id)]?.next_lesson));return matches&&keep});count.textContent=materials.length+'曲 × '+data.students.length+'人';sheet.innerHTML=data.students.length?`<table><thead><tr><th class="song-head">曲名</th>${data.students.map(s=>`<th class="student">${esc(s.display_name||'名前未登録')}<label class="student-set"><select onchange="setInstrument('${esc(s.user_id)}',this.value)"><option value="">楽器: すべて</option><option value="ウクレレ"${s.instrument==='ウクレレ'?' selected':''}>ウクレレのみ</option><option value="ギター"${s.instrument==='ギター'?' selected':''}>ギターのみ</option></select></label></th>`).join('')}</tr></thead><tbody>${materials.map(m=>`<tr><td class="song"><div class="t"><b>${esc(m.title)}</b>${m.artist?`<em>${esc(m.artist)}</em>`:''}</div><span>${tags(m)}${videos(m)}</span></td>${data.students.map(s=>cell(m,s)).join('')}</tr>`).join('')}</tbody></table>`:'<div class="empty">生徒がまだ登録されていません</div>'}
function cell(m,s){let p=progress[key(s.user_id,m.id)],st=stateOf(p),d=lessonDate(p);return `<td class="cell" onclick="openEditor('${esc(s.user_id)}',${m.id})"><div class="cell-content"><div class="status-block"><span class="${st}">${stateLabel(st)}</span>${d?`<span class="lesson-date">${jaDate(d)}</span>`:''}${p?.next_lesson?'<span class="nextmark">▶ 次回</span>':''}</div>${p?.student_note?`<span class="memo-preview" title="${esc(p.student_note)}">${esc(p.student_note)}</span>`:''}</div></td>`}
async function setInstrument(uid,instrument){try{let r=await fetch('/admin/carte/student',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_id:uid,instrument})}),d=await r.json();if(!r.ok)throw Error(d.error||'保存に失敗しました');let s=data.students.find(x=>x.user_id===uid);if(s)s.instrument=instrument;notice.textContent=instrument?instrument+'の曲だけを表示する設定にしました':'すべての楽器を表示する設定にしました';notice.style.display='block';setTimeout(()=>notice.style.display='none',2200)}catch(e){alert(e.message);load()}}
function openEditor(uid,mid){let s=data.students.find(x=>x.user_id===uid),m=data.materials.find(x=>x.id===mid),p=progress[key(uid,mid)];editing={uid,mid};editSong.textContent=m.title;editStudent.textContent=(s.display_name||'名前未登録')+'さん';document.querySelector(`input[name="state"][value="${stateOf(p)}"]`).checked=true;nextLesson.checked=!!p?.next_lesson;lessonDate.value=lessonDate(p);studentNote.value=p?.student_note||'';editor.showModal()}
async function save(e){e.preventDefault();let st=document.querySelector('input[name="state"]:checked')?.value||'notdone',done=st==='done',button=saveButton;button.disabled=true;button.textContent='保存中';let body={user_id:editing.uid,material_id:editing.mid,lesson_done:done,lesson_date:lessonDate.value,student_note:studentNote.value,next_lesson:nextLesson.checked,status:done?'completed':(st==='wanted'?'wanted':'planned')};try{let r=await fetch('/admin/carte/progress',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),d=await r.json();if(!r.ok)throw Error(d.error||'保存失敗');progress[key(editing.uid,editing.mid)]=d.progress;editor.close();draw();notice.textContent='保存しました';notice.style.display='block';setTimeout(()=>notice.style.display='none',1800)}catch(err){alert(err.message)}finally{button.disabled=false;button.textContent='保存'}}
q.oninput=draw;filter.onchange=draw;editor.addEventListener('click',e=>{if(e.target===editor)editor.close()});load().catch(e=>sheet.innerHTML='<div class="empty">'+esc(e.message)+'</div>');
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
function draw(){let f=filter.value,xs=items.filter(x=>f==='all'||(x.status||'open')===f);count.textContent=xs.length+'件';list.innerHTML=xs.length?`<table><thead><tr><th>曲</th><th>ひとこと</th><th>リクエスト</th><th>私も</th><th>状態</th><th></th></tr></thead><tbody>${xs.map(x=>`<tr><td class="song"><b>${esc(x.title)}</b>${x.artist?`<em>${esc(x.artist)}</em>`:''}${x.instrument?`<br><span class="tag ${x.instrument==='ウクレレ'?'uk':'gt'}">${esc(x.instrument)}</span>`:''}</td><td>${x.comment?`<span class="comment">${esc(x.comment)}</span>`:''}</td><td class="who">${esc(x.display_name||'名前未登録')}<br><span class="when">${esc(jaTime(x.created_at))}</span></td><td class="votes">${(x.votes||[]).length}人</td><td><select class="status" onchange="setStatus('${esc(x.id)}',this.value)"><option value="open"${(x.status||'open')==='open'?' selected':''}>未対応</option><option value="added"${x.status==='added'?' selected':''}>追加済み</option><option value="declined"${x.status==='declined'?' selected':''}>見送り</option></select></td><td><button class="del" onclick="remove('${esc(x.id)}','${esc(x.title)}')">削除</button></td></tr>`).join('')}</tbody></table>`:'<div class="empty">該当するリクエストはありません</div>'}
function toast(t){notice.textContent=t;notice.style.display='block';setTimeout(()=>notice.style.display='none',2000)}
async function send(body){let r=await fetch('/admin/carte/request',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),d=await r.json();if(!r.ok)throw Error(d.error||'保存に失敗しました');await load()}
async function setStatus(id,status){try{await send({id,status});toast('状態を変えました')}catch(e){alert(e.message)}}
async function remove(id,title){if(!confirm('「'+title+'」を削除します。よろしいですか？'))return;try{await send({id,delete:true});toast('削除しました')}catch(e){alert(e.message)}}
async function load(){let r=await fetch('/admin/carte/data'),d=await r.json();if(!r.ok)throw Error(d.error||'取得に失敗しました');items=(d.requests||[]).slice().reverse();draw()}
filter.onchange=draw;load().catch(e=>{list.innerHTML='<div class="empty">'+esc(e.message)+'</div>'});
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
        materials = load_materials()
        # 講師が生徒の楽器を設定していれば、その楽器の曲だけを見せる（未設定なら全曲）
        instrument = _prefs().get(user_id, {}).get("instrument", "")
        if instrument:
            materials = [m for m in materials if m["instrument"] == instrument]
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
        rows = _requests()
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

    @bp.post("/admin/carte/student")
    def set_student_instrument():
        """生徒ごとの楽器を設定する。設定すると、その生徒の画面にはその楽器の曲だけが出る。"""
        require_admin()
        body = request.get_json(silent=True) or {}
        user_id = str(body.get("user_id") or "")
        if not any(m.get("user_id") == user_id for m in load_json("members", default=[])):
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

    @bp.post("/admin/carte/sync")
    def sync_materials():
        require_admin()
        return {"ok": True, "count": len(load_materials(force=True))}

    return bp
