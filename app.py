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
from urllib.parse import quote

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
# 管理画面の見出しに表示する名前（関西用・関東用など複数運用時の見分け用）
PANEL_NAME = os.environ.get("PANEL_NAME", "日程調整Bot")
# 回答フォームで選ばせる教室。「|」区切りで指定する（未設定なら教室選択は表示しない）
LOCATIONS = [s.strip() for s in os.environ.get("LOCATIONS", "").split("|") if s.strip()]

# 生徒がテキストを送ってきたときの自動返信。
# このBotは日程調整専用なので、それ以外の連絡は本アカウントへ誘導する。
# 環境変数 AUTO_REPLY で差し替え可能（地域ごとに連絡先が違うため）。
DEFAULT_AUTO_REPLY = "\n".join([
    "このアカウントは日程調整の専用です。",
    "",
    "日程のご回答は、お送りしたメッセージの「日程を選ぶ」ボタンからお願いします。",
    "",
    "レッスンに関するご連絡・ご質問は、お手数ですが本アカウントまでお願いします。",
])
AUTO_REPLY = os.environ.get("AUTO_REPLY", "").strip() or DEFAULT_AUTO_REPLY


def format_date_ja(value: str) -> str:
    """'2026-08-05' → '8/5(火)'。変換できない場合はそのまま返す。"""
    try:
        d = datetime.strptime(value, "%Y-%m-%d")
    except (ValueError, TypeError):
        return value or ""
    return f"{d.month}/{d.day}({'月火水木金土日'[d.weekday()]})"

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
  input[type=checkbox], input[type=radio] { margin-right: 10px; transform: scale(1.3); }
  .dayblock { border-top: 1px solid #eee; padding: 12px 0 4px; }
  .dayname { font-size: 15px; font-weight: 600; margin-bottom: 8px; }
  .chips { display: flex; flex-wrap: wrap; gap: 7px; }
  .chip { position: relative; }
  .chip input { position: absolute; opacity: 0; width: 0; height: 0; }
  .chip span { display: inline-block; padding: 9px 15px; border-radius: 20px; border: 1px solid #ccc;
               font-size: 15px; background: #fff; color: #444; cursor: pointer; }
  .chip input:checked + span { background: #06C755; border-color: #06C755; color: #fff; font-weight: 600; }
  .daycount { font-size: 12px; color: #06C755; margin-left: 8px; font-weight: normal; }
  button { width: 100%; padding: 14px; font-size: 16px; background: #06C755; color: #fff; border: none; border-radius: 8px; margin-top: 16px; }
  #status { margin-top: 12px; color: #666; }
  h2 { font-size: 15px; margin: 22px 0 6px; }
  h2 .req { font-size: 11px; color: #fff; background: #e05252; border-radius: 4px; padding: 2px 6px; margin-left: 6px; vertical-align: middle; }
  textarea { width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 8px; font-size: 15px; font-family: inherit; resize: vertical; }
  #deadline { background: #fff6e5; border: 1px solid #f0d9a8; border-radius: 8px; padding: 10px 12px; font-size: 14px; margin-bottom: 14px; display: none; }
  #editNote { background: #eaf3ff; border: 2px solid #4a90d9; border-radius: 10px;
              padding: 14px; margin-bottom: 16px; display: none; }
  #editNote .ttl { font-size: 17px; font-weight: 700; color: #1a5c9e; display: flex; align-items: center; gap: 8px; }
  #editNote .ttl .mk { display: inline-block; width: 24px; height: 24px; border-radius: 50%;
                       background: #4a90d9; color: #fff; font-size: 15px; line-height: 24px; text-align: center; }
  #editNote .sub { font-size: 14px; color: #33506e; margin-top: 8px; line-height: 1.5; }
  #done { display: none; }
  .donebox { background: #f0f9f3; border: 2px solid #06C755; border-radius: 12px; padding: 20px 16px; text-align: center; }
  .donemark { width: 52px; height: 52px; border-radius: 50%; background: #06C755; color: #fff;
              font-size: 30px; line-height: 52px; margin: 0 auto 10px; }
  .donetitle { font-size: 18px; font-weight: 600; color: #06783b; }
  .donesub { font-size: 13px; color: #555; margin-top: 6px; }
  .donelist { text-align: left; background: #fff; border: 1px solid #dceee4; border-radius: 8px;
              padding: 12px 14px; margin-top: 14px; font-size: 14px; }
  .donelist .sec { font-weight: 600; margin: 10px 0 4px; }
  .donelist .sec:first-child { margin-top: 0; }
  .donelist .day { font-weight: 600; margin-top: 8px; }
  .donelist .tm { margin-left: 12px; }
  button.edit { background: #fff; color: #06783b; border: 1px solid #06C755; }
</style>
</head>
<body>
<div id="deadline"></div>

<div id="done">
  <div class="donebox">
    <div class="donemark">✓</div>
    <div class="donetitle">送信が完了しました</div>
    <div class="donesub">この内容で受け付けました。ページを閉じてOKです。</div>
    <div class="donelist" id="doneList"></div>
  </div>
  <button class="edit" onclick="backToEdit()">内容を修正する</button>
</div>

<div id="formArea">
<div id="editNote"></div>
<h1>都合の良い日程をすべて選んでください</h1>
<form id="form"></form>

<div id="locationBox" style="display:none">
  <h2>教室<span class="req">必須</span></h2>
  <div id="locations"></div>
</div>

<div id="commentBox" style="display:none">
  <h2 id="commentTitle">（任意）連絡事項やリクエストあれば</h2>
  <textarea id="comment" rows="3" maxlength="500"></textarea>
</div>

<button id="submitBtn">この内容で送信する</button>
<div id="status">読み込み中...</div>
</div>

<script>
const LIFF_ID = "__LIFF_ID__";
let candidates = [];
let idToken = "";

function updateDayCounts() {
  document.querySelectorAll("[data-day-count]").forEach(el => {
    const day = el.getAttribute("data-day-count");
    const n = document.querySelectorAll(`input[data-day="${day}"]:checked`).length;
    el.textContent = n ? `　${n}件選択中` : "";
  });
}

async function main() {
  await liff.init({ liffId: LIFF_ID });
  if (!liff.isLoggedIn()) {
    liff.login();
    return;
  }
  idToken = liff.getIDToken();
  const res = await fetch("/liff/candidates");
  const data = await res.json();
  candidates = data.candidates || [];
  const form = document.getElementById("form");

  if (data.deadline) {
    const box = document.getElementById("deadline");
    box.textContent = "回答期限: " + data.deadline;
    box.style.display = "block";
  }

  if (candidates.length === 0) {
    document.getElementById("status").textContent = "現在、回答可能な日程候補がありません。";
    document.getElementById("submitBtn").style.display = "none";
    return;
  }

  const locs = data.locations || [];
  if (locs.length) {
    document.getElementById("locations").innerHTML = locs.map((l, i) => `
      <label>
        <input type="radio" name="location" value="${l}">${l}
      </label>
    `).join("");
    document.getElementById("locationBox").style.display = "block";
  }
  // 日付ごとにまとめ、時刻はタップ式のチップで並べる
  const byDay = [];
  candidates.forEach((c, i) => {
    const sp = c.lastIndexOf(" ");
    const day = sp > 0 ? c.slice(0, sp) : c;
    const time = sp > 0 ? c.slice(sp + 1) : "";
    let g = byDay.find(x => x.day === day);
    if (!g) { g = { day: day, items: [] }; byDay.push(g); }
    g.items.push({ index: i + 1, time: time });
  });

  form.innerHTML = byDay.map(g => `
    <div class="dayblock">
      <div class="dayname">${g.day}<span class="daycount" data-day-count="${g.day}"></span></div>
      <div class="chips">
        ${g.items.map(it => `
          <label class="chip">
            <input type="checkbox" name="candidate" value="${it.index}" data-day="${g.day}">
            <span>${it.time}</span>
          </label>
        `).join("")}
      </div>
    </div>
  `).join("");

  form.addEventListener("change", updateDayCounts);

  if (data.comment_label) {
    document.getElementById("commentTitle").textContent = data.comment_label;
  }
  document.getElementById("commentBox").style.display = "block";
  document.getElementById("status").textContent = "";

  await loadPrevious();
  updateDayCounts();
}

async function loadPrevious() {
  try {
    const res = await fetch("/liff/answers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ idToken: idToken }),
    });
    if (!res.ok) return;
    const prev = await res.json();
    if (!prev.answered) return;

    (prev.selected || []).forEach(i => {
      const el = document.querySelector('input[name="candidate"][value="' + i + '"]');
      if (el) el.checked = true;
    });
    if (prev.location) {
      const el = document.querySelector('input[name="location"][value="' + prev.location + '"]');
      if (el) el.checked = true;
    }
    if (prev.comment) document.getElementById("comment").value = prev.comment;

    const note = document.getElementById("editNote");
    note.innerHTML =
      '<div class="ttl"><span class="mk">✓</span>回答済みです</div>' +
      '<div class="sub">前回の内容を表示しています。<br>変更する場合は選び直して、下の「この内容に更新する」を押してください。</div>';
    note.style.display = "block";
    document.getElementById("submitBtn").textContent = "この内容に更新する";
  } catch (e) {}
}

function showDone() {
  const picked = Array.from(document.querySelectorAll('input[name="candidate"]:checked'))
    .map(el => candidates[parseInt(el.value, 10) - 1]);
  const locEl = document.querySelector('input[name="location"]:checked');
  const comment = document.getElementById("comment").value.trim();

  const parts = [];
  parts.push('<div class="sec">選んだ日時（' + picked.length + '件）</div>');
  if (picked.length) {
    let lastDay = "";
    picked.forEach(c => {
      const sp = c.lastIndexOf(" ");
      const day = sp > 0 ? c.slice(0, sp) : c;
      const time = sp > 0 ? c.slice(sp + 1) : "";
      if (day !== lastDay) { parts.push('<div class="day">' + day + "</div>"); lastDay = day; }
      parts.push('<div class="tm">' + time + "</div>");
    });
  } else {
    parts.push("<div>なし</div>");
  }
  if (locEl) parts.push('<div class="sec">教室</div><div>' + locEl.value + "</div>");
  if (comment) parts.push('<div class="sec">連絡事項</div><div>' + comment + "</div>");

  document.getElementById("doneList").innerHTML = parts.join("");
  document.getElementById("formArea").style.display = "none";
  document.getElementById("done").style.display = "block";
  window.scrollTo(0, 0);
}

function backToEdit() {
  document.getElementById("done").style.display = "none";
  document.getElementById("formArea").style.display = "block";
  document.getElementById("status").textContent = "";
  document.getElementById("submitBtn").textContent = "この内容に更新する";
  window.scrollTo(0, 0);
}

document.getElementById("submitBtn").addEventListener("click", async () => {
  const checked = Array.from(document.querySelectorAll('input[name="candidate"]:checked')).map(el => parseInt(el.value, 10));

  const locBox = document.getElementById("locationBox");
  const locEl = document.querySelector('input[name="location"]:checked');
  if (locBox.style.display !== "none" && !locEl) {
    document.getElementById("status").textContent = "教室を選択してください。";
    locBox.scrollIntoView({ block: "center" });
    return;
  }

  document.getElementById("status").textContent = "送信中...";
  try {
    const res = await fetch("/liff/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        idToken: idToken,
        selected: checked,
        comment: document.getElementById("comment").value,
        location: locEl ? locEl.value : "",
      }),
    });
    const data = await res.json();
    if (res.ok) {
      document.getElementById("status").textContent = "";
      showDone();
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
    return {
        "candidates": candidates,
        "locations": LOCATIONS,
        "deadline": format_date_ja(load_json("deadline", default="")),
        "comment_label": load_json("comment_label", default="") or "（任意）連絡事項やリクエストあれば",
    }


def verify_liff_user(id_token: str):
    """LIFFのIDトークンをLINE側で検証して (user_id, 表示名) を返す。
    検証できない場合は (None, エラーメッセージ)。"""
    if not id_token:
        return None, "idTokenがありません。"

    verify_res = requests.post(
        "https://api.line.me/oauth2/v2.1/verify",
        data={"id_token": id_token, "client_id": LINE_CHANNEL_ID},
        timeout=10,
    )
    if verify_res.status_code != 200:
        return None, "認証に失敗しました。もう一度LINEアプリ内からフォームを開き直してください。"

    payload = verify_res.json()
    user_id = payload.get("sub", "")
    if not user_id:
        return None, "ユーザー情報を取得できませんでした。"
    return user_id, payload.get("name", user_id)


@app.route("/liff/answers", methods=["POST"])
def liff_answers():
    """前回の回答内容を返す（フォームを開き直したときに反映するため）"""
    body = request.get_json(silent=True) or {}
    user_id, name_or_error = verify_liff_user(body.get("idToken", ""))
    if not user_id:
        return {"error": name_or_error}, 401

    selected = [v["candidate_index"] for v in load_json("votes") if v["user_id"] == user_id]
    location = next(
        (l["location"] for l in load_json("locations") if l["user_id"] == user_id), ""
    )
    comment = next(
        (c["text"] for c in load_json("comments") if c["user_id"] == user_id), ""
    )
    return {
        "answered": bool(selected or location or comment),
        "selected": sorted(selected),
        "location": location,
        "comment": comment,
    }


@app.route("/liff/submit", methods=["POST"])
def liff_submit():
    """LIFFフォームからの投票送信を受け取る。idTokenをLINE側のverifyエンドポイントで検証し、
    なりすましを防いだ上でこのユーザーの投票を今回選択した内容で上書きする。"""
    body = request.get_json(silent=True) or {}
    selected = body.get("selected", [])

    user_id, display_name = verify_liff_user(body.get("idToken", ""))
    if not user_id:
        return {"error": display_name}, 401

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

    # 教室（設定されている場合は必須）
    if LOCATIONS:
        location = (body.get("location") or "").strip()
        if location not in LOCATIONS:
            return {"error": "教室を選択してください。"}, 400
        locations = [l for l in load_json("locations") if l["user_id"] != user_id]
        locations.append(
            {"user_id": user_id, "display_name": display_name, "location": location}
        )
        save_json("locations", locations)

    # 自由記述（任意）。同じ人が再送信したら上書きする。
    comment = (body.get("comment") or "").strip()[:500]
    comments = [c for c in load_json("comments") if c["user_id"] != user_id]
    if comment:
        comments.append(
            {
                "user_id": user_id,
                "display_name": display_name,
                "text": comment,
                "timestamp": datetime.now().isoformat(),
            }
        )
    save_json("comments", comments)

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
<title>__PANEL_NAME__ 管理画面</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans", sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; color: #222; background: #fafafa; }
  h1 { font-size: 20px; }
  h2 { font-size: 15px; margin: 24px 0 8px; padding-bottom: 6px; border-bottom: 2px solid #06C755; }
  .card { background: #fff; border: 1px solid #e2e2e2; border-radius: 10px; padding: 16px; }
  .row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  button { padding: 9px 14px; font-size: 14px; border: none; border-radius: 6px; cursor: pointer; background: #06C755; color: #fff; }
  button.sub { background: #eee; color: #333; }
  button.mini { background: #eee; color: #444; padding: 4px 10px; font-size: 12px; }

  .wkhead { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; font-weight: 600; font-size: 15px; }
  .wk { display: grid; grid-template-columns: 52px repeat(7, 1fr); gap: 2px; user-select: none; touch-action: none; }
  .wk .hd { text-align: center; font-size: 12px; padding: 5px 0 7px; font-weight: 600; cursor: pointer; border-radius: 6px; line-height: 1.4; }
  .wk .hd:hover { background: #f0f9f3; }
  .wk .hd.sat { color: #3b7dd8; }
  .wk .hd.sun { color: #d84b4b; }
  .wk .hd.today { background: #eaf7ef; }
  .wk .hd.pasthd { color: #ccc; cursor: default; }
  .wk .hd.pasthd:hover { background: none; }
  .wk .tl { font-size: 11px; color: #999; text-align: right; padding-right: 6px; line-height: 26px; }
  .wk .cell { height: 26px; border: 1px solid #e8e8e8; border-radius: 4px; background: #fff; cursor: pointer; }
  .wk .cell:hover { border-color: #9bdcb5; }
  .wk .cell.sel { background: #06C755; border-color: #06C755; }
  .wk .cell.past { background: #f6f6f6; border-color: #f0f0f0; cursor: default; }
  .wk .cell.past:hover { border-color: #f0f0f0; }

  textarea, input[type=text] { width: 100%; padding: 10px; border: 1px solid #ccc; border-radius: 6px; font-size: 14px; font-family: inherit; resize: vertical; }
  .dlcal { background: #f8f9fa; border: 1px solid #e6e8ea; border-radius: 10px; padding: 12px 14px; }
  .calhead { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; font-weight: 600; font-size: 14px; }
  .calgrid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 4px; }
  .calgrid .wd { text-align: center; font-size: 12px; color: #888; padding: 3px 0; }
  .calgrid .wd.sat { color: #3b7dd8; }
  .calgrid .wd.sun { color: #d84b4b; }
  .dlday { padding: 8px 0; text-align: center; font-size: 14px; border: 1px solid #e4e4e4; border-radius: 8px; background: #fff; cursor: pointer; user-select: none; }
  .dlday:hover { border-color: #06C755; }
  .dlday.sel { background: #06C755; color: #fff; border-color: #06C755; font-weight: 600; }
  .dlday.today { border-color: #06C755; border-width: 2px; }
  .dlday.past { color: #c8c8c8; background: #fafafa; cursor: default; }
  .dlday.past:hover { border-color: #e4e4e4; }
  .dlday.blank { border: none; background: none; cursor: default; }
  .bubble { max-width: 300px; border: 1px solid #e0e0e0; border-radius: 10px; padding: 12px; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,.06); margin-bottom: 14px; }
  .bubbletext { font-size: 13px; font-weight: 600; white-space: pre-wrap; word-break: break-word; }
  .bubblesub { font-size: 11px; color: #999; margin-top: 4px; }
  .bubblebtn { margin-top: 10px; background: #06C755; color: #fff; text-align: center; padding: 9px; border-radius: 6px; font-size: 13px; font-weight: 600; }
  #preview { list-style: none; padding: 0; margin: 0; max-height: 260px; overflow-y: auto; }
  #preview li { padding: 6px 4px; border-bottom: 1px solid #f0f0f0; font-size: 14px; }
  #preview li.head { font-weight: 600; color: #06783b; background: #f6fbf8; border-bottom: none; padding-top: 10px; }
  .muted { color: #888; font-size: 13px; }
  #result { margin-top: 10px; padding: 12px; border-radius: 8px; white-space: pre-wrap; font-size: 13px; display: none; }
  #result.ok { background: #f0f9f3; border: 1px solid #b6e2c6; display: block; }
  #result.err { background: #fdf2f2; border: 1px solid #e0b4b4; display: block; }
  .member { display: flex; justify-content: space-between; align-items: center; padding: 6px 4px; font-size: 14px; border-bottom: 1px solid #f4f4f4; }
  .member .qwrap { font-size: 12px; color: #888; white-space: nowrap; }
  .member .quota { width: 52px; padding: 5px; margin-left: 6px; border: 1px solid #ccc; border-radius: 6px; font-size: 14px; text-align: center; }
  .member .group { width: 110px; padding: 5px; margin: 0 10px 0 6px; border: 1px solid #ccc; border-radius: 6px; font-size: 14px; }
  .links a { font-size: 13px; margin-right: 14px; }
</style>
</head>
<body>
<h1>__PANEL_NAME__ 管理画面</h1>

<h2>1. 候補の時間をドラッグで選ぶ</h2>
<div class="card">
  <div class="wkhead">
    <button class="mini" onclick="moveWeek(-1)">‹ 前の週</button>
    <span id="wkTitle"></span>
    <button class="mini" onclick="moveWeek(1)">次の週 ›</button>
  </div>
  <div class="wk" id="grid"></div>
  <div class="row" style="margin-top:12px">
    <span class="muted">ドラッグで範囲選択・日付をクリックでその日を一括選択／解除できます</span>
    <button class="mini" onclick="clearAll()">すべて解除</button>
  </div>
</div>

<h2>2. メッセージと回答期限</h2>
<div class="card">
  <textarea id="message" rows="3" placeholder="メンバーに表示されるメッセージ"></textarea>
  <div class="row" style="margin-top:8px">
    <span class="muted">候補ボタンの上に表示されます</span>
    <button class="mini" onclick="resetMessage()">初期文に戻す</button>
  </div>

  <div class="lbl" style="margin-top:18px">回答フォームの記入欄の見出し</div>
  <input type="text" id="commentLabel" oninput="onMessageInput()" placeholder="（任意）連絡事項やリクエストあれば">
  <div class="row" style="margin-top:6px">
    <button class="mini" onclick="resetCommentLabel()">初期文に戻す</button>
  </div>

  <div class="lbl" style="margin-top:18px">回答期限</div>
  <div class="dlcal">
    <div class="calhead">
      <button class="mini" onclick="moveDlMonth(-1)">‹ 前の月</button>
      <span id="dlTitle"></span>
      <button class="mini" onclick="moveDlMonth(1)">次の月 ›</button>
    </div>
    <div class="calgrid" id="dlWeekdays"></div>
    <div class="calgrid" id="dlDays"></div>
    <div class="row" style="margin-top:10px">
      <span class="muted" id="dlChosen"></span>
      <button class="mini" onclick="clearDeadline()">なしにする</button>
    </div>
  </div>
</div>

<h2>3. 送信内容のプレビュー</h2>
<div class="card">
  <div class="bubble">
    <div class="bubbletext" id="msgPreview"></div>
    <div class="bubblesub" id="count">候補: 0件</div>
    <div class="bubblebtn">日程を選ぶ</div>
  </div>
  <div class="muted" id="deadlinePreview" style="margin:-6px 0 12px"></div>
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
    <a href="#" onclick="go('summarize');return false;">回答を集計する</a>
    <a href="#" onclick="runAssign();return false;">時間枠を自動で割り当てる</a>
    <a href="#" onclick="if(confirm('回答データを削除します。よろしいですか？'))go('reset');return false;">回答をリセットする</a>
  </div>
</div>

<h2>6. 確定した日程を各自に送る</h2>
<div class="card">
  <div class="lbl">通知メッセージの前置き</div>
  <textarea id="notifyMessage" rows="2" oninput="saveNotifyMessage()"></textarea>
  <div class="row" style="margin-top:8px">
    <button class="sub" onclick="goNotify()">送信内容を確認する</button>
    <button class="mini" onclick="resetNotifyMessage()">初期文に戻す</button>
  </div>
  <div class="muted" style="margin-top:8px">
    各自に自分の枠だけが届きます。確認画面が出てから送信されます。
  </div>

</div>

<script>
const TOKEN = new URLSearchParams(location.search).get('token') || '';
const WD = ['日','月','火','水','木','金','土'];
// 10:00〜22:00（開始時刻を変えたい場合は下の 10 と length を調整する）
const HOURS = Array.from({ length: 13 }, (_, i) => String(i + 10).padStart(2, '0') + ':00');

let selected = new Set();      // "2026-08-03 14:00"
let weekStart = startOfWeek(new Date());
let dragging = false, dragMode = 'on';

function ymd(dt) {
  return dt.getFullYear() + '-' + String(dt.getMonth() + 1).padStart(2, '0') + '-' + String(dt.getDate()).padStart(2, '0');
}
function fmt(d) {
  const dt = new Date(d + 'T00:00:00');
  return (dt.getMonth() + 1) + '/' + dt.getDate() + '(' + WD[dt.getDay()] + ')';
}
function startOfWeek(dt) {
  const d = new Date(dt); d.setHours(0, 0, 0, 0); d.setDate(d.getDate() - d.getDay()); return d;
}
function weekDays() {
  return Array.from({ length: 7 }, (_, i) => {
    const d = new Date(weekStart); d.setDate(weekStart.getDate() + i); return ymd(d);
  });
}
function moveWeek(diff) { weekStart.setDate(weekStart.getDate() + diff * 7); renderGrid(); }
function clearAll() { selected.clear(); renderGrid(); renderPreview(); }

function renderGrid() {
  const days = weekDays();
  const today = ymd(new Date());
  document.getElementById('wkTitle').textContent = fmt(days[0]) + ' 〜 ' + fmt(days[6]);

  let html = '<div class="tl"></div>';
  days.forEach((d, i) => {
    const cls = ['hd'];
    if (i === 0) cls.push('sun');
    if (i === 6) cls.push('sat');
    if (d === today) cls.push('today');
    if (d < today) cls.push('pasthd');
    const click = d < today ? '' : ` onclick="toggleDay('${d}')"`;
    html += `<div class="${cls.join(' ')}"${click}>${fmt(d).replace('(', '<br>(')}</div>`;
  });

  for (const h of HOURS) {
    html += `<div class="tl">${h}</div>`;
    for (const d of days) {
      const key = d + ' ' + h;
      const past = d < today;
      const cls = 'cell' + (selected.has(key) ? ' sel' : '') + (past ? ' past' : '');
      html += `<div class="${cls}"${past ? '' : ` data-key="${key}"`}></div>`;
    }
  }
  document.getElementById('grid').innerHTML = html;
}
function toggleDay(d) {
  const keys = HOURS.map(h => d + ' ' + h);
  const allOn = keys.every(k => selected.has(k));
  keys.forEach(k => allOn ? selected.delete(k) : selected.add(k));
  renderGrid(); renderPreview();
}
function applyCell(el) {
  const key = el.dataset.key;
  if (!key) return;
  if (dragMode === 'on') { selected.add(key); el.classList.add('sel'); }
  else { selected.delete(key); el.classList.remove('sel'); }
}
const DEFAULT_MESSAGE = '日程調整のお願いです。ボタンをタップして、都合の良い日程をすべて選んでください。';
const msgBox = document.getElementById('message');

const DEFAULT_COMMENT_LABEL = '（任意）連絡事項やリクエストあれば';
let deadlineValue = '';
let dlView = new Date();

function currentMessage() { return msgBox.value.trim() || DEFAULT_MESSAGE; }
function currentCommentLabel() {
  return document.getElementById('commentLabel').value.trim() || DEFAULT_COMMENT_LABEL;
}
function deadlineJa() {
  if (!deadlineValue) return '';
  const dt = new Date(deadlineValue + 'T00:00:00');
  return (dt.getMonth() + 1) + '/' + dt.getDate() + '(' + WD[dt.getDay()] + ')';
}
function renderDeadlineCal() {
  const y = dlView.getFullYear(), mo = dlView.getMonth();
  document.getElementById('dlTitle').textContent = y + '年' + (mo + 1) + '月';
  document.getElementById('dlWeekdays').innerHTML = WD.map((w, i) =>
    `<div class="wd ${i === 0 ? 'sun' : i === 6 ? 'sat' : ''}">${w}</div>`).join('');

  const first = new Date(y, mo, 1).getDay();
  const last = new Date(y, mo + 1, 0).getDate();
  const todayStr = ymd(new Date());
  let cells = Array.from({ length: first }, () => '<div class="dlday blank"></div>');
  for (let d = 1; d <= last; d++) {
    const s = ymd(new Date(y, mo, d));
    const cls = ['dlday'];
    if (s === deadlineValue) cls.push('sel');
    if (s === todayStr) cls.push('today');
    if (s < todayStr) cls.push('past');
    const click = s < todayStr ? '' : ` onclick="pickDeadline('${s}')"`;
    cells.push(`<div class="${cls.join(' ')}"${click}>${d}</div>`);
  }
  document.getElementById('dlDays').innerHTML = cells.join('');
  const ja = deadlineJa();
  document.getElementById('dlChosen').textContent = ja ? '選択中: ' + ja : '未設定';
}
function moveDlMonth(diff) { dlView.setMonth(dlView.getMonth() + diff); renderDeadlineCal(); }
function pickDeadline(s) { deadlineValue = (deadlineValue === s) ? '' : s; onMessageInput(); }
function clearDeadline() { deadlineValue = ''; onMessageInput(); }

function onMessageInput() {
  document.getElementById('msgPreview').textContent = currentMessage();
  const d = deadlineJa();
  document.getElementById('deadlinePreview').textContent = d ? '本文の最後に「回答期限: ' + d + '」が入ります' : '';
  renderDeadlineCal();
  try {
    localStorage.setItem('scheduleBotMessage', msgBox.value);
    localStorage.setItem('scheduleBotDeadline', deadlineValue);
    localStorage.setItem('scheduleBotCommentLabel', document.getElementById('commentLabel').value);
  } catch (e) {}
}
function resetMessage() { msgBox.value = DEFAULT_MESSAGE; onMessageInput(); }
function resetCommentLabel() { document.getElementById('commentLabel').value = DEFAULT_COMMENT_LABEL; onMessageInput(); }
function initMessage() {
  let saved = '', savedDl = '', savedLabel = '';
  try {
    saved = localStorage.getItem('scheduleBotMessage') || '';
    savedDl = localStorage.getItem('scheduleBotDeadline') || '';
    savedLabel = localStorage.getItem('scheduleBotCommentLabel') || '';
  } catch (e) {}
  msgBox.value = saved || DEFAULT_MESSAGE;
  document.getElementById('commentLabel').value = savedLabel || DEFAULT_COMMENT_LABEL;
  deadlineValue = savedDl || '';
  if (deadlineValue) dlView = new Date(deadlineValue + 'T00:00:00');
  msgBox.addEventListener('input', onMessageInput);
  onMessageInput();
}

function renderPreview() {
  const keys = Array.from(selected).sort();
  document.getElementById('count').textContent = '候補: ' + keys.length + '件';
  if (!keys.length) {
    document.getElementById('preview').innerHTML = '<li class="muted">グリッドをドラッグすると、ここに候補が表示されます</li>';
    return;
  }
  let html = '', lastDay = '', n = 0;
  for (const k of keys) {
    const [d, t] = k.split(' ');
    if (d !== lastDay) { html += `<li class="head">${fmt(d)}</li>`; lastDay = d; }
    n++;
    html += `<li>${n}. ${fmt(d)} ${t}</li>`;
  }
  document.getElementById('preview').innerHTML = html;
}
function candidates() {
  return Array.from(selected).sort().map(k => {
    const [d, t] = k.split(' ');
    return fmt(d) + ' ' + t;
  });
}

// --- ドラッグ選択（マウス／タッチ） ---
const grid = document.getElementById('grid');
grid.addEventListener('mousedown', e => {
  const el = e.target.closest('.cell[data-key]');
  if (!el) return;
  e.preventDefault();
  dragging = true;
  dragMode = selected.has(el.dataset.key) ? 'off' : 'on';
  applyCell(el);
});
grid.addEventListener('mouseover', e => {
  if (!dragging) return;
  const el = e.target.closest('.cell[data-key]');
  if (el) applyCell(el);
});
document.addEventListener('mouseup', () => {
  if (dragging) { dragging = false; renderPreview(); }
});
grid.addEventListener('touchstart', e => {
  const el = document.elementFromPoint(e.touches[0].clientX, e.touches[0].clientY);
  const cell = el && el.closest('.cell[data-key]');
  if (!cell) return;
  e.preventDefault();
  dragging = true;
  dragMode = selected.has(cell.dataset.key) ? 'off' : 'on';
  applyCell(cell);
}, { passive: false });
grid.addEventListener('touchmove', e => {
  if (!dragging) return;
  e.preventDefault();
  const el = document.elementFromPoint(e.touches[0].clientX, e.touches[0].clientY);
  const cell = el && el.closest('.cell[data-key]');
  if (cell) applyCell(cell);
}, { passive: false });
document.addEventListener('touchend', () => {
  if (dragging) { dragging = false; renderPreview(); }
});

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
      `<div class="member">
         <label><input type="checkbox" value="${i + 1}" checked> ${i + 1}. ${m.name}</label>
         <span class="qwrap">
           グループ <input class="group" type="text" placeholder="個人" value="${m.group || ''}">
           コマ数 <input class="quota" type="number" min="0" max="9" value="${m.quota}">
         </span>
       </div>`
    ).join('');
  } catch (e) {
    document.getElementById('members').textContent = 'メンバー取得に失敗しました: ' + e;
  }
}
async function send() {
  const list = candidates();
  const box = document.getElementById('result');
  if (!list.length) { box.className = 'err'; box.textContent = '候補が0件です。グリッドから時間を選んでください。'; return; }
  const to = Array.from(document.querySelectorAll('#members input[type=checkbox]:checked')).map(cb => cb.value);
  if (!to.length) { box.className = 'err'; box.textContent = '送信先が選ばれていません。'; return; }
  if (!confirm(list.length + '件の候補を' + to.length + '人に送信します。よろしいですか？')) return;

  const btn = document.getElementById('sendBtn');
  btn.disabled = true; btn.textContent = '送信中...';
  box.className = ''; box.style.display = 'none';
  try {
    const url = '/admin/send?token=' + encodeURIComponent(TOKEN)
      + '&candidates=' + encodeURIComponent(list.join('|'))
      + '&to=' + to.join(',')
      + '&message=' + encodeURIComponent(currentMessage())
      + '&deadline=' + encodeURIComponent(deadlineValue)
      + '&comment_label=' + encodeURIComponent(currentCommentLabel());
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
const DEFAULT_NOTIFY = 'レッスン日程が確定しましたのでお知らせします。';
function notifyBox() { return document.getElementById('notifyMessage'); }
function saveNotifyMessage() {
  try { localStorage.setItem('scheduleBotNotify', notifyBox().value); } catch (e) {}
}
function resetNotifyMessage() { notifyBox().value = DEFAULT_NOTIFY; saveNotifyMessage(); }
function initNotify() {
  let saved = '';
  try { saved = localStorage.getItem('scheduleBotNotify') || ''; } catch (e) {}
  notifyBox().value = saved || DEFAULT_NOTIFY;
}
function goNotify() {
  const msg = notifyBox().value.trim() || DEFAULT_NOTIFY;
  location.href = '/admin/notify?token=' + encodeURIComponent(TOKEN)
    + '&message=' + encodeURIComponent(msg);
}

function runAssign() {
  const qs = Array.from(document.querySelectorAll('#members .quota')).map(i => i.value || '1');
  if (!qs.length) { alert('メンバーがまだ登録されていません。'); return; }
  const gs = Array.from(document.querySelectorAll('#members .group')).map(i => i.value.trim());
  location.href = '/admin/assign?token=' + encodeURIComponent(TOKEN)
    + '&quotas=' + qs.join(',')
    + '&groups=' + encodeURIComponent(gs.join('|'));
}

initMessage();
initNotify();
renderGrid();
renderPreview();
loadMembers();
</script>
</body>
</html>
"""


@app.route("/admin/panel", methods=["GET"])
def admin_panel():
    """管理者用の入力フォーム。日付と時間帯を選ぶだけで候補リストを組み立てて送信できる。"""
    check_admin_token()
    return ADMIN_PANEL_HTML.replace("__PANEL_NAME__", PANEL_NAME)


@app.route("/admin/members.json", methods=["GET"])
def admin_members_json():
    """管理画面が送信先とコマ数を描画するためのメンバー一覧"""
    check_admin_token()
    members = load_json("members")
    quotas = load_json("quotas", default={})
    groups = load_json("groups", default={})
    return {
        "members": [
            {
                "name": m["display_name"],
                "quota": int(quotas.get(m["user_id"], 1)),
                "group": groups.get(m["user_id"], ""),
            }
            for m in members
        ]
    }


@app.route("/admin/assign", methods=["GET"])
def admin_assign():
    """?token=...&quotas=1,2,1&groups=|A班|A班 の形式で、回答をもとに時間枠を自動割り当てする。
    quotas（カンマ区切り）とgroups（縦棒区切り）はメンバー一覧の並び順に対応する。
    グループ名が同じ人は1組として扱い、空欄なら個人レッスン扱い。"""
    check_admin_token()
    import assign as assign_mod

    members = load_json("members")
    counts = [int(x) for x in request.args.get("quotas", "").split(",") if x.strip().isdigit()]
    group_raw = request.args.get("groups", "")
    group_names = group_raw.split("|") if group_raw else []

    quotas, groups = {}, {}
    for i, mem in enumerate(members):
        quotas[mem["user_id"]] = counts[i] if i < len(counts) else 1
        groups[mem["user_id"]] = group_names[i].strip() if i < len(group_names) else ""
    save_json("quotas", quotas)
    save_json("groups", groups)

    candidates = load_json("candidates", default=[])
    votes = load_json("votes")
    locations = {l["user_id"]: l["location"] for l in load_json("locations")}
    result = assign_mod.auto_assign(candidates, votes, quotas, locations, groups)

    # 通知で使えるように結果を保存しておく
    save_json("assignment", result.get("schedule", []))

    panel = f"/admin/panel?token={ADMIN_TOKEN}"
    body = assign_mod.format_result(result)
    notify = f"/admin/notify?token={ADMIN_TOKEN}"
    links = f'<p><a href="{notify}">この内容を各自にLINEで送る</a>　<a href="{panel}">管理画面に戻る</a></p>'
    return f"<pre>{body}</pre>{links}"


@app.route("/admin/notify", methods=["GET"])
def admin_notify():
    """割り当て結果を各自にLINEで通知する。
    確認なしで送らないよう、まずプレビューを表示し &send=1 で実際に送信する。"""
    check_admin_token()
    from schedule_tools import build_notifications, send_notifications, DEFAULT_NOTIFY_MESSAGE

    schedule = load_json("assignment", default=[])
    if not schedule:
        return (
            "<pre>送信できる割り当て結果がありません。先に「時間枠を自動で割り当てる」を実行してください。</pre>"
            f'<p><a href="/admin/panel?token={ADMIN_TOKEN}">管理画面に戻る</a></p>'
        )

    message = request.args.get("message", "") or load_json("notify_message", default="")
    save_json("notify_message", message)
    items = build_notifications(schedule, message)

    if request.args.get("send") == "1":
        result = send_notifications(items)
        return (
            f"<pre>{result}</pre>"
            f'<p><a href="/admin/panel?token={ADMIN_TOKEN}">管理画面に戻る</a></p>'
        )

    total = sum(len(i["user_ids"]) for i in items)
    preview = [f"{total}通を送信します。内容を確認してください。", ""]
    for i in items:
        preview.append(f"── {i['name']}（{len(i['user_ids'])}人）")
        preview.append(i["text"])
        preview.append("")

    send_url = (
        f"/admin/notify?token={ADMIN_TOKEN}&send=1&message={quote(message or DEFAULT_NOTIFY_MESSAGE)}"
    )
    return (
        f"<pre>{chr(10).join(preview)}</pre>"
        f'<p><a href="{send_url}" '
        f"onclick=\"return confirm('{total}通を送信します。よろしいですか？')\">"
        f"この内容で送信する</a>　"
        f'<a href="/admin/panel?token={ADMIN_TOKEN}">やめる</a></p>'
    )


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

    message = request.args.get("message", "")

    # 締め切り日（YYYY-MM-DD）。フォーム上部とLINE本文の末尾に表示する。
    deadline_raw = request.args.get("deadline", "").strip()
    save_json("deadline", deadline_raw)

    # 回答フォームの記入欄の見出し
    comment_label = request.args.get("comment_label", "").strip()
    save_json("comment_label", comment_label)

    result = send_schedule(candidates, member_indices, message, format_date_ja(deadline_raw))
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
    """テキストが届いたら、日程調整専用である旨と連絡先を案内する。
    Reply APIなので何通返しても無料通数は消費しない。"""
    user_id = event.source.user_id

    with ApiClient(configuration) as api_client:
        line_api = MessagingApi(api_client)
        display_name = get_display_name(line_api, user_id)
        upsert_member(user_id, display_name)

        line_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=AUTO_REPLY)],
            )
        )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
