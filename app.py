"""
LINE公式アカウント 日程調整Bot - Webhookサーバー（タップ投票版）

役割:
- LINEからのメッセージ・友だち追加・ボタンタップ(postback)イベントを受け取るWebhookエンドポイント
- 友だち追加してくれたメンバーの情報を members.json に記録
- メンバーが日程候補ボタンをタップすると votes.json に記録（もう一度タップで選択解除のトグル式）
- すべての応答はReply API経由（無料通数にカウントされない）

日程の一斉送信・集計は schedule_tools.py で行う（このサーバーとは別に実行する）。
"""

import os
import requests
from datetime import datetime

from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, FollowEvent, PostbackEvent

from storage import load_json, save_json

CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
LIFF_ID = os.environ.get("LIFF_ID", "")
LINE_CHANNEL_ID = os.environ.get("LINE_CHANNEL_ID", "")

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

app = Flask(__name__)


def upsert_member(user_id, display_name):
    members = load_json("members")
    for m in members:
        if m["user_id"] == user_id:
            m["display_name"] = display_name
            save_json("members", members)
            return
    members.append({"user_id": user_id, "display_name": display_name})
    save_json("members", members)


def get_display_name(line_api, user_id):
    try:
        profile = line_api.get_profile(user_id)
        return profile.display_name
    except Exception:
        return user_id


def toggle_vote(user_id, display_name, candidate_index):
    """候補へのタップをトグルする。戻り値: (選択された=True / 解除された=False)"""
    votes = load_json("votes")
    for v in votes:
        if v["user_id"] == user_id and v["candidate_index"] == candidate_index:
            votes.remove(v)
            save_json("votes", votes)
            return False
    votes.append(
        {
            "user_id": user_id,
            "display_name": display_name,
            "candidate_index": candidate_index,
            "timestamp": datetime.now().isoformat(),
        }
    )
    save_json("votes", votes)
    return True


@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@app.route("/", methods=["GET"])
def health_check():
    # Renderのスリープ解除やヘルスチェック用
    return "LINE schedule bot is running."


LIFF_PAGE_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>日程調整</title>
<script charset="utf-8" src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; padding: 16px; }
  h1 { font-size: 18px; }
  label { display: block; padding: 12px; margin-bottom: 8px; border: 1px solid #ddd; border-radius: 8px; }
  input[type=checkbox] { margin-right: 10px; transform: scale(1.3); }
  button { width: 100%; padding: 14px; font-size: 16px; background: #06C755; color: #fff; border: none; border-radius: 8px; margin-top: 16px; }
  #status { margin-top: 12px; color: #666; }
</style>
</head>
<body>
<h1>都合の良い日程をすべて選んでください</h1>
<form id="form"></form>
<button id="submitBtn">この内容で送信する</button>
<div id="status">読み込み中...</div>

<script>
const LIFF_ID = "__LIFF_ID__";
let candidates = [];

async function main() {
  await liff.init({ liffId: LIFF_ID });
  if (!liff.isLoggedIn()) {
    liff.login();
    return;
  }
  const res = await fetch("/liff/candidates");
  const data = await res.json();
  candidates = data.candidates || [];
  const form = document.getElementById("form");
  if (candidates.length === 0) {
    document.getElementById("status").textContent = "現在、回答可能な日程候補がありません。";
    document.getElementById("submitBtn").style.display = "none";
    return;
  }
  form.innerHTML = candidates.map((c, i) => `
    <label>
      <input type="checkbox" name="candidate" value="${i + 1}">${c}
    </label>
  `).join("");
  document.getElementById("status").textContent = "";
}

document.getElementById("submitBtn").addEventListener("click", async () => {
  const checked = Array.from(document.querySelectorAll('input[name="candidate"]:checked')).map(el => parseInt(el.value, 10));
  document.getElementById("status").textContent = "送信中...";
  try {
    const idToken = liff.getIDToken();
    const res = await fetch("/liff/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ idToken: idToken, selected: checked }),
    });
    const data = await res.json();
    if (res.ok) {
      document.getElementById("status").textContent = "送信しました。このページを閉じてOKです。";
    } else {
      document.getElementById("status").textContent = "エラー: " + (data.error || "送信に失敗しました。");
    }
  } catch (e) {
    document.getElementById("status").textContent = "エラーが発生しました: " + e;
  }
});

main().catch(e => {
  document.getElementById("status").textContent = "初期化エラー: " + e;
});
</script>
</body>
</html>
"""


@app.route("/liff", methods=["GET"])
def liff_page():
    """LIFF(LINEアプリ内ブラウザ)で開くチェックボックス式の投票フォーム。
    候補数が多いとき(schedule_tools.LIFF_THRESHOLD超)はこちらへのリンクを送る。"""
    return LIFF_PAGE_HTML.replace("__LIFF_ID__", LIFF_ID)


@app.route("/liff/candidates", methods=["GET"])
def liff_candidates():
    """LIFFフォームが現在の候補一覧を取得するための公開エンドポイント(メンバー用・認証不要・読み取り専用)"""
    candidates = load_json("candidates", default=[])
    return {"candidates": candidates}


@app.route("/liff/submit", methods=["POST"])
def liff_submit():
    """LIFFフォームからの投票送信を受け取る。idTokenをLINE側のverifyエンドポイントで検証し、
    なりすましを防いだ上でこのユーザーの投票を今回選択した内容で上書きする。"""
    body = request.get_json(silent=True) or {}
    id_token = body.get("idToken", "")
    selected = body.get("selected", [])

    if not id_token:
        return {"error": "idTokenがありません。"}, 400

    verify_res = requests.post(
        "https://api.line.me/oauth2/v2.1/verify",
        data={"id_token": id_token, "client_id": LINE_CHANNEL_ID},
        timeout=10,
    )
    if verify_res.status_code != 200:
        return {"error": "認証に失敗しました。もう一度LINEアプリ内からフォームを開き直してください。"}, 401

    payload = verify_res.json()
    user_id = payload.get("sub", "")
    display_name = payload.get("name", user_id)
    if not user_id:
        return {"error": "ユーザー情報を取得できませんでした。"}, 401

    upsert_member(user_id, display_name)

    candidates = load_json("candidates", default=[])
    valid_selected = [i for i in selected if isinstance(i, int) and 0 < i <= len(candidates)]

    votes = load_json("votes")
    votes = [v for v in votes if v["user_id"] != user_id]
    for idx in valid_selected:
        votes.append(
            {
                "user_id": user_id,
                "display_name": display_name,
                "candidate_index": idx,
                "timestamp": datetime.now().isoformat(),
            }
        )
    save_json("votes", votes)

    return {"ok": True, "selected": valid_selected}


def check_admin_token():
    """無料プランはShellが使えないため、ブラウザから叩けるURLで管理操作を行う。
    ?token=... にADMIN_TOKENと一致する値がないと403にする。"""
    token = request.args.get("token", "")
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        abort(403)


ADMIN_PANEL_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>日程調整Bot 管理画面</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans", sans-serif; max-width: 820px; margin: 0 auto; padding: 20px; color: #222; background: #fafafa; }
  h1 { font-size: 20px; }
  h2 { font-size: 15px; margin: 24px 0 8px; padding-bottom: 6px; border-bottom: 2px solid #06C755; }
  .card { background: #fff; border: 1px solid #e2e2e2; border-radius: 10px; padding: 16px; margin-bottom: 4px; }
  .row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  input[type=date], input[type=text] { padding: 8px; border: 1px solid #ccc; border-radius: 6px; font-size: 14px; }
  button { padding: 9px 14px; font-size: 14px; border: none; border-radius: 6px; cursor: pointer; background: #06C755; color: #fff; }
  button.sub { background: #eee; color: #333; }
  button.danger { background: #fff; color: #c00; border: 1px solid #e0b4b4; padding: 2px 8px; font-size: 12px; }
  .chip { display: inline-flex; align-items: center; gap: 6px; background: #f0f9f3; border: 1px solid #b6e2c6; border-radius: 20px; padding: 5px 12px; font-size: 13px; margin: 3px 3px 0 0; }
  .slot { display: inline-block; }
  .slot input { display: none; }
  .slot span { display: inline-block; padding: 7px 12px; border: 1px solid #ccc; border-radius: 20px; font-size: 13px; cursor: pointer; margin: 3px 3px 0 0; background: #fff; }
  .slot input:checked + span { background: #06C755; color: #fff; border-color: #06C755; }
  #preview { list-style: none; padding: 0; margin: 0; max-height: 260px; overflow-y: auto; }
  #preview li { display: flex; justify-content: space-between; align-items: center; padding: 7px 4px; border-bottom: 1px solid #f0f0f0; font-size: 14px; }
  .muted { color: #888; font-size: 13px; }
  #result { margin-top: 10px; padding: 12px; border-radius: 8px; white-space: pre-wrap; font-size: 13px; display: none; }
  #result.ok { background: #f0f9f3; border: 1px solid #b6e2c6; display: block; }
  #result.err { background: #fdf2f2; border: 1px solid #e0b4b4; display: block; }
  label.member { display: block; padding: 7px 4px; font-size: 14px; }
  .links a { font-size: 13px; margin-right: 14px; }
</style>
</head>
<body>
<h1>日程調整Bot 管理画面</h1>

<h2>1. 日付を選ぶ</h2>
<div class="card">
  <div class="row">
    <input type="date" id="dateInput">
    <button class="sub" onclick="addDate()">日付を追加</button>
    <button class="sub" onclick="addWeekdays()">今後2週間の平日を一括追加</button>
  </div>
  <div id="dateChips" style="margin-top:10px"></div>
</div>

<h2>2. 時間帯を選ぶ（複数可）</h2>
<div class="card">
  <div id="slots"></div>
  <div class="row" style="margin-top:12px">
    <input type="text" id="customSlot" placeholder="例: 18:30-20:00">
    <button class="sub" onclick="addCustomSlot()">時間帯を追加</button>
  </div>
</div>

<h2>3. 候補リストのプレビュー</h2>
<div class="card">
  <div class="muted" id="count">候補: 0件</div>
  <ul id="preview"></ul>
</div>

<h2>4. 送信先を選ぶ</h2>
<div class="card">
  <div class="row" style="margin-bottom:8px">
    <button class="sub" onclick="toggleAll(true)">全員選択</button>
    <button class="sub" onclick="toggleAll(false)">全員解除</button>
  </div>
  <div id="members" class="muted">読み込み中...</div>
</div>

<h2>5. 送信</h2>
<div class="card">
  <button onclick="send()" id="sendBtn">この内容でLINEに送信する</button>
  <div id="result"></div>
  <div class="links" style="margin-top:14px">
    <a href="#" onclick="go('summarize');return false;">投票を集計する</a>
    <a href="#" onclick="if(confirm('投票データを削除します。よろしいですか？'))go('reset');return false;">投票をリセットする</a>
  </div>
</div>

<script>
const TOKEN = new URLSearchParams(location.search).get('token') || '';
const DEFAULT_SLOTS = ['10:00-', '11:00-', '13:00-', '14:00-', '15:00-', '16:00-', '18:00-', '19:00-'];
let dates = [];
let slots = DEFAULT_SLOTS.map(s => ({ label: s, checked: false }));
const WD = ['日','月','火','水','木','金','土'];

function fmt(d) {
  const dt = new Date(d + 'T00:00:00');
  return (dt.getMonth() + 1) + '/' + dt.getDate() + '(' + WD[dt.getDay()] + ')';
}
function addDate() {
  const v = document.getElementById('dateInput').value;
  if (v && !dates.includes(v)) { dates.push(v); dates.sort(); render(); }
}
function addWeekdays() {
  const today = new Date();
  for (let i = 1; i <= 14; i++) {
    const d = new Date(today); d.setDate(today.getDate() + i);
    if (d.getDay() === 0 || d.getDay() === 6) continue;
    const s = d.toISOString().slice(0, 10);
    if (!dates.includes(s)) dates.push(s);
  }
  dates.sort(); render();
}
function removeDate(d) { dates = dates.filter(x => x !== d); render(); }
function addCustomSlot() {
  const el = document.getElementById('customSlot');
  const v = el.value.trim();
  if (v && !slots.some(s => s.label === v)) { slots.push({ label: v, checked: true }); el.value = ''; render(); }
}
function candidates() {
  const chosen = slots.filter(s => s.checked).map(s => s.label);
  const out = [];
  for (const d of dates) for (const t of chosen) out.push(fmt(d) + ' ' + t);
  return out;
}
function render() {
  document.getElementById('dateChips').innerHTML = dates.length
    ? dates.map(d => `<span class="chip">${fmt(d)}<button class="danger" onclick="removeDate('${d}')">×</button></span>`).join('')
    : '<span class="muted">まだ日付が選ばれていません</span>';

  document.getElementById('slots').innerHTML = slots.map((s, i) =>
    `<label class="slot"><input type="checkbox" ${s.checked ? 'checked' : ''} onchange="slots[${i}].checked=this.checked;render()"><span>${s.label}</span></label>`
  ).join('');

  const list = candidates();
  document.getElementById('count').textContent = '候補: ' + list.length + '件';
  document.getElementById('preview').innerHTML = list.length
    ? list.map((c, i) => `<li><span>${i + 1}. ${c}</span></li>`).join('')
    : '<li class="muted">日付と時間帯を選ぶとここに表示されます</li>';
}
function toggleAll(on) {
  document.querySelectorAll('#members input[type=checkbox]').forEach(cb => cb.checked = on);
}
async function loadMembers() {
  try {
    const res = await fetch('/admin/members.json?token=' + encodeURIComponent(TOKEN));
    const data = await res.json();
    const box = document.getElementById('members');
    if (!data.members || !data.members.length) {
      box.innerHTML = '<span class="muted">メンバーがまだ登録されていません。先に友だち追加してもらってください。</span>';
      return;
    }
    box.className = '';
    box.innerHTML = data.members.map((m, i) =>
      `<label class="member"><input type="checkbox" value="${i + 1}" checked> ${i + 1}. ${m}</label>`
    ).join('');
  } catch (e) {
    document.getElementById('members').textContent = 'メンバー取得に失敗しました: ' + e;
  }
}
async function send() {
  const list = candidates();
  const box = document.getElementById('result');
  if (!list.length) { box.className = 'err'; box.textContent = '候補が0件です。日付と時間帯を選んでください。'; return; }
  const to = Array.from(document.querySelectorAll('#members input[type=checkbox]:checked')).map(cb => cb.value);
  if (!to.length) { box.className = 'err'; box.textContent = '送信先が選ばれていません。'; return; }
  if (!confirm(list.length + '件の候補を' + to.length + '人に送信します。よろしいですか？')) return;

  const btn = document.getElementById('sendBtn');
  btn.disabled = true; btn.textContent = '送信中...';
  box.className = ''; box.style.display = 'none';
  try {
    const url = '/admin/send?token=' + encodeURIComponent(TOKEN)
      + '&candidates=' + encodeURIComponent(list.join('|'))
      + '&to=' + to.join(',');
    const res = await fetch(url);
    const text = await res.text();
    box.className = res.ok ? 'ok' : 'err';
    box.textContent = text.replace(/<[^>]*>/g, '');
  } catch (e) {
    box.className = 'err'; box.textContent = 'エラー: ' + e;
  }
  btn.disabled = false; btn.textContent = 'この内容でLINEに送信する';
}
function go(path) { location.href = '/admin/' + path + '?token=' + encodeURIComponent(TOKEN); }

render();
loadMembers();
</script>
</body>
</html>
"""


@app.route("/admin/panel", methods=["GET"])
def admin_panel():
    """管理者用の入力フォーム。日付と時間帯を選ぶだけで候補リストを組み立てて送信できる。"""
    check_admin_token()
    return ADMIN_PANEL_HTML


@app.route("/admin/members.json", methods=["GET"])
def admin_members_json():
    """管理画面が送信先チェックボックスを描画するためのメンバー一覧（表示名のみ）"""
    check_admin_token()
    members = load_json("members")
    return {"members": [m["display_name"] for m in members]}


@app.route("/admin/members", methods=["GET"])
def admin_members():
    """?token=... にアクセスすると登録メンバー一覧を番号付きで表示する（送信先を絞るときに使う番号）"""
    check_admin_token()
    from schedule_tools import list_members

    result = list_members()
    return f"<pre>{result}</pre>"


@app.route("/admin/send", methods=["GET"])
def admin_send():
    """?token=...&candidates=候補1|候補2|候補3 の形式でアクセスすると日程候補を一斉送信する。
    &to=1,3 のように/admin/membersで確認した番号を指定すると送信先を絞り込める（省略時は全員）。"""
    check_admin_token()
    from schedule_tools import send_schedule

    candidates_raw = request.args.get("candidates", "")
    candidates = [c.strip() for c in candidates_raw.split("|") if c.strip()]
    if not candidates:
        return "候補が指定されていません。?candidates=候補1|候補2|候補3 の形式で指定してください。", 400

    to_raw = request.args.get("to", "")
    member_indices = [int(x) for x in to_raw.split(",") if x.strip().isdigit()] or None

    result = send_schedule(candidates, member_indices)
    return f"<pre>{result}</pre>"


@app.route("/admin/summarize", methods=["GET"])
def admin_summarize():
    """?token=... にアクセスすると現在の投票状況を集計して表示する"""
    check_admin_token()
    from schedule_tools import summarize_replies

    result = summarize_replies()
    return f"<pre>{result}</pre>"


@app.route("/admin/reset", methods=["GET"])
def admin_reset():
    """?token=... にアクセスすると投票データをリセットする（次回の日程調整の前に使う）"""
    check_admin_token()
    from schedule_tools import reset_replies

    result = reset_replies()
    return f"<pre>{result}</pre>"


@handler.add(FollowEvent)
def handle_follow(event):
    """メンバーが公式アカウントを友だち追加したときに一覧へ登録"""
    user_id = event.source.user_id
    with ApiClient(configuration) as api_client:
        line_api = MessagingApi(api_client)
        display_name = get_display_name(line_api, user_id)
        upsert_member(user_id, display_name)
        line_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[
                    TextMessage(
                        text=f"{display_name}さん、友だち追加ありがとうございます！日程調整の際にご案内します。"
                    )
                ],
            )
        )


@handler.add(PostbackEvent)
def handle_postback(event):
    """日程候補ボタンのタップを処理（votes.jsonに記録し、選択/解除をトグル）"""
    user_id = event.source.user_id
    data = dict(pair.split("=") for pair in event.postback.data.split("&"))

    if data.get("action") != "vote":
        return

    candidate_index = int(data["candidate"])
    candidates = load_json("candidates", default=[])

    with ApiClient(configuration) as api_client:
        line_api = MessagingApi(api_client)
        display_name = get_display_name(line_api, user_id)
        upsert_member(user_id, display_name)

        selected = toggle_vote(user_id, display_name, candidate_index)

        label = candidates[candidate_index - 1] if 0 < candidate_index <= len(candidates) else f"候補{candidate_index}"
        reply_text = f"「{label}」を選択しました。" if selected else f"「{label}」の選択を解除しました。"

        line_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)],
            )
        )


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    """テキストで返信してきた人にはボタンでの回答をお願いする案内を返す"""
    user_id = event.source.user_id

    with ApiClient(configuration) as api_client:
        line_api = MessagingApi(api_client)
        display_name = get_display_name(line_api, user_id)
        upsert_member(user_id, display_name)

        line_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[
                    TextMessage(
                        text="日程候補メッセージのボタンをタップしてご回答ください（複数タップ可）。"
                    )
                ],
            )
        )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
