"""
LINE公式アカウント 日程調整Bot - Webhookサーバー（タップ投票版）

役割:
- LINEからのメッセージ・友だち追加・ボタンタップ(postback)イベントを受け取るWebhookエンドポイント
- 友だち追加してくれたメンバーの情報を members.json に記録
- メンバーが日程候補ボタンをタップすると votes.json に記録（もう一度タップで選択解除のトグル式）
- すべての応答はReply API経由（無料通数にカウントされない）

日程の一斉送信・集計は schedule_tools.py で行う（このサーバーとは別に実行する）。
"""

import hmac
import os
import threading
import time
import requests
from datetime import datetime
from urllib.parse import urlencode

from flask import Flask, request, abort, g
from werkzeug.local import LocalProxy
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    PushMessageRequest,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, FollowEvent, PostbackEvent

from storage import load_json, save_json
from tenant_config import DEFAULT_TENANT, TENANTS, get_tenant

CHANNEL_ACCESS_TOKEN = LocalProxy(lambda: get_tenant().channel_access_token)
CHANNEL_SECRET = LocalProxy(lambda: get_tenant().channel_secret)
ADMIN_TOKEN = LocalProxy(lambda: get_tenant().admin_token)
LIFF_ID = LocalProxy(lambda: get_tenant().liff_id)
LINE_CHANNEL_ID = LocalProxy(lambda: get_tenant().line_channel_id)
# 管理画面の見出しに表示する名前（関西用・関東用など複数運用時の見分け用）
PANEL_NAME = LocalProxy(lambda: get_tenant().panel_name)
# 回答フォームで選ばせる教室。「|」区切りで指定する（未設定なら教室選択は表示しない）
LOCATIONS = LocalProxy(lambda: list(get_tenant().locations))

# 生徒がテキストを送ってきたときの自動返信。
# このBotは日程調整専用なので、それ以外の連絡は本アカウントへ誘導する。
# 環境変数 AUTO_REPLY で差し替え可能（地域ごとに連絡先が違うため）。
DEFAULT_AUTO_REPLY = "\n".join([
    "このアカウントは、レッスンの予定と曲の記録用です。",
    "",
    "画面下の「メニュー」から、次のレッスンの確認・日程の回答・マイレパートリー・曲のリクエストができます。",
    "",
    "レッスンのご相談・ご質問は、メニューの「先生に連絡」からメインのアカウントへお願いします。",
])
AUTO_REPLY = LocalProxy(lambda: get_tenant().auto_reply)

# --- 地域ごとの機能ON/OFF -------------------------------------------------
# 同じコードを関西・関東の両方で動かし、環境変数だけで使う機能を切り替える。
#
#   CARTE_LIFF_ID    … 生徒カルテ用のLIFF ID。設定されていればカルテを有効にする
#   CARTE_ENABLED    … カルテを明示的にON/OFFしたいとき（未設定ならLIFF IDの有無で判断）
#   SCHEDULE_ENABLED … 日程調整を使わない地域ではfalseにする（未設定ならON）
#
# 既存の環境変数はそのまま。何も足さなければ従来どおり日程調整だけが動く。


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


CARTE_LIFF_ID = LocalProxy(lambda: get_tenant().carte_liff_id)
CARTE_ENABLED = LocalProxy(lambda: get_tenant().carte_enabled)
SCHEDULE_ENABLED = LocalProxy(lambda: get_tenant().schedule_enabled)

# 日程調整をOFFにしたときに閉じるパス。カルテと共通の /healthz などは閉じない。
SCHEDULE_PATHS = ("/liff", "/admin/panel", "/admin/members", "/admin/assign",
                  "/admin/schedule", "/admin/notify", "/admin/send",
                  "/admin/summarize", "/admin/reset", "/admin/keepalive")


def format_date_ja(value: str) -> str:
    """'2026-08-05' → '8/5(火)'。変換できない場合はそのまま返す。"""
    try:
        d = datetime.strptime(value, "%Y-%m-%d")
    except (ValueError, TypeError):
        return value or ""
    return f"{d.month}/{d.day}({'月火水木金土日'[d.weekday()]})"

configurations = {
    name: Configuration(access_token=config.channel_access_token)
    for name, config in TENANTS.items()
}
handlers = {
    name: WebhookHandler(config.channel_secret)
    for name, config in TENANTS.items()
}
configuration = LocalProxy(lambda: configurations[get_tenant().name])


class _TenantHandlers:
    """Register callbacks on every LINE channel, then dispatch per request."""

    def add(self, *args, **kwargs):
        def decorator(func):
            for item in handlers.values():
                item.add(*args, **kwargs)(func)
            return func
        return decorator

    def handle(self, body, signature):
        return handlers[get_tenant().name].handle(body, signature)


handler = _TenantHandlers()

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
  .bulk { display: none; gap: 8px; margin: 4px 0 6px; }
  .bulk button { width: auto; flex: 1; margin: 0; padding: 10px; font-size: 14px; background: #fff;
                 color: #06783b; border: 1px solid #06C755; border-radius: 8px; }
  .bulk button.off { color: #666; border-color: #ccc; }
  .dayname { display: flex; align-items: center; }
  .dayall { margin-left: auto; width: auto; padding: 4px 10px; margin-top: 0; font-size: 12px;
            background: #fff; color: #06783b; border: 1px solid #b6e2c6; border-radius: 14px; }
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

<label id="skipBox" style="border:1px solid #e0c0c0;background:#fdf6f6">
  <input type="checkbox" id="skip" onchange="onSkipChange()">今回は参加できません
</label>

<div class="bulk" id="bulk">
  <button type="button" onclick="selectAll(true)">すべて選択</button>
  <button type="button" class="off" onclick="selectAll(false)">すべて解除</button>
</div>

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
  document.querySelectorAll("[data-day-all]").forEach(btn => {
    const els = dayInputs(btn.dataset.dayAll);
    btn.textContent = els.length && els.every(el => el.checked) ? "この日を解除" : "この日すべて";
  });
}

// LIFFはIDトークンをブラウザに保存して使い回す。期限（約1時間）が切れたものを
// そのまま送ると、以後ずっと「認証に失敗しました」になる。切れていたらログインし直す。
function liffTokenExp(t) {
  try {
    return JSON.parse(atob(t.split('.')[1].replace(/-/g, '+').replace(/_/g, '/'))).exp * 1000;
  } catch (e) { return 0; }
}
function liffTokenFresh(t) { return !!t && liffTokenExp(t) - 60000 > Date.now(); }
// 入り直せたら true。短時間に繰り返すと無限ループになるので30秒に1回まで。
function liffRelogin() {
  let last = 0;
  try { last = Number(sessionStorage.getItem('liffRelogin') || 0); } catch (e) {}
  if (Date.now() - last < 30000) return false;
  try { sessionStorage.setItem('liffRelogin', String(Date.now())); } catch (e) {}
  // LINEアプリ内では logout が効かない。開き直せば新しいトークンが来る。
  if (liff.isInClient()) { location.reload(); return true; }
  try { liff.logout(); } catch (e) {}
  liff.login({ redirectUri: location.href });
  return true;
}

async function main() {
  await liff.init({ liffId: LIFF_ID });
  if (!liff.isLoggedIn()) {
    liff.login({ redirectUri: location.href });
    return;
  }
  idToken = liff.getIDToken();
  if (!liffTokenFresh(idToken) && liffRelogin()) return;
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
      <div class="dayname">${g.day}<span class="daycount" data-day-count="${g.day}"></span>
        <button type="button" class="dayall" data-day-all="${g.day}" onclick="toggleDay(this.dataset.dayAll)">この日すべて</button></div>
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
  document.getElementById("bulk").style.display = "flex";

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
    if (prev.skip) { document.getElementById("skip").checked = true; onSkipChange(); }

    const note = document.getElementById("editNote");
    note.innerHTML =
      '<div class="ttl"><span class="mk">✓</span>回答済みです</div>' +
      '<div class="sub">前回の内容を表示しています。<br>変更する場合は選び直して、下の「この内容に更新する」を押してください。</div>';
    note.style.display = "block";
    document.getElementById("submitBtn").textContent = "この内容に更新する";
  } catch (e) {}
}

function dayInputs(day) {
  return Array.from(document.querySelectorAll('input[name="candidate"]'))
    .filter(el => el.dataset.day === day);
}
function selectAll(on) {
  if (document.getElementById("skip").checked) return;
  document.querySelectorAll('input[name="candidate"]').forEach(el => el.checked = on);
  updateDayCounts();
}
function toggleDay(day) {
  if (document.getElementById("skip").checked) return;
  const els = dayInputs(day);
  const allOn = els.length > 0 && els.every(el => el.checked);
  els.forEach(el => el.checked = !allOn);
  updateDayCounts();
}

function onSkipChange() {
  const off = document.getElementById("skip").checked;
  document.getElementById("form").style.opacity = off ? "0.4" : "1";
  document.getElementById("form").style.pointerEvents = off ? "none" : "auto";
  const locBox = document.getElementById("locationBox");
  if (locBox) {
    locBox.style.opacity = off ? "0.4" : "1";
    locBox.style.pointerEvents = off ? "none" : "auto";
  }
  const bulk = document.getElementById("bulk");
  bulk.style.opacity = off ? "0.4" : "1";
  bulk.style.pointerEvents = off ? "none" : "auto";
  if (off) {
    document.querySelectorAll('input[name="candidate"]:checked').forEach(el => el.checked = false);
    updateDayCounts();
  }
}

function showDone() {
  if (document.getElementById("skip").checked) {
    const comment = document.getElementById("comment").value.trim();
    let html = '<div class="sec">今回は参加できません</div>';
    if (comment) html += '<div class="sec">連絡事項</div><div>' + comment + "</div>";
    document.getElementById("doneList").innerHTML = html;
    document.getElementById("formArea").style.display = "none";
    document.getElementById("done").style.display = "block";
    window.scrollTo(0, 0);
    return;
  }
  showDonePicked();
}

function showDonePicked() {
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

  const skip = document.getElementById("skip").checked;
  const locBox = document.getElementById("locationBox");
  const locEl = document.querySelector('input[name="location"]:checked');
  if (!skip && locBox.style.display !== "none" && !locEl) {
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
        selected: skip ? [] : checked,
        comment: document.getElementById("comment").value,
        location: skip ? "" : (locEl ? locEl.value : ""),
        skip: skip,
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
    return LIFF_PAGE_HTML.replace("__LIFF_ID__", str(LIFF_ID))


@app.route("/liff/candidates", methods=["GET"])
def liff_candidates():
    """LIFFフォームが現在の候補一覧を取得するための公開エンドポイント(メンバー用・認証不要・読み取り専用)"""
    candidates = load_json("candidates", default=[])
    return {
        "candidates": candidates,
        "locations": list(LOCATIONS),
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
        data={"id_token": id_token, "client_id": str(LINE_CHANNEL_ID)},
        timeout=10,
    )
    if verify_res.status_code != 200:
        # LINEが理由を返してくれるので、そのまま出す（調査のとき原因がすぐ分かる）。
        detail = ""
        try:
            detail = str(verify_res.json().get("error_description", ""))
        except Exception:
            pass
        if "expired" in detail.lower():
            # 画面側が自動でログインし直すので、待つだけでよい。
            return None, "ログインの有効期限が切れました。読み込み直しています…"
        suffix = f"（{detail}）" if detail else ""
        return None, f"認証に失敗しました。もう一度LINEアプリ内からフォームを開き直してください。{suffix}"

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
    skipped = user_id in load_json("skips")
    return {
        "answered": bool(selected or location or comment or skipped),
        "selected": sorted(selected),
        "location": location,
        "comment": comment,
        "skip": skipped,
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

    # 「今回は参加できません」— 未回答と区別するために記録する
    skip = bool(body.get("skip"))
    skips = [s for s in load_json("skips") if s != user_id]
    if skip:
        skips.append(user_id)
    save_json("skips", skips)

    # 教室（設定されている場合は必須。欠席の場合は不要）
    if LOCATIONS and not skip:
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
    from admin_auth import teacher_session_ok

    if teacher_session_ok():
        return
    # POSTの確認画面から来る場合はフォームの隠しフィールドに入っている
    token = request.args.get("token", "") or request.form.get("token", "")
    admin_token = str(ADMIN_TOKEN)
    if not admin_token or not hmac.compare_digest(token, admin_token):
        abort(403)


def admin_link(path: str, **params) -> str:
    """Build admin links without exposing the token for cookie sessions."""
    from admin_auth import teacher_session_ok

    values = {key: value for key, value in params.items() if value not in (None, "")}
    if not teacher_session_ok():
        values["token"] = str(ADMIN_TOKEN)
    url = f"/admin/{path.lstrip('/')}"
    return url + ("?" + urlencode(values) if values else "")


def admin_form_token() -> str:
    from admin_auth import teacher_session_ok

    return "" if teacher_session_ok() else str(ADMIN_TOKEN)


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
  .member .group { width: 110px; padding: 5px; margin: 0 10px 0 6px; border: 1px solid #ccc; border-radius: 6px; font-size: 14px; background: #fff; }
  .gnew { display: flex; gap: 8px; margin: 4px 0 12px; }
  .gnew input { flex: 1; min-width: 0; padding: 8px; border: 1px solid #ccc; border-radius: 8px; font-size: 14px; }
  .gbox { border: 1px solid #e3e3e3; border-radius: 10px; margin-bottom: 10px; overflow: hidden; }
  .gbox.solo { border-style: dashed; }
  .ghead { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 8px 10px; background: #f6fbf8; font-weight: 600; font-size: 14px; }
  .gbox.solo .ghead { background: #fafafa; }
  .ghead label { display: flex; align-items: center; gap: 6px; cursor: pointer; }
  .ghead .gcount { font-weight: 400; color: #888; font-size: 12px; }
  .ghead .gdel { background: none; border: none; color: #b55; font-size: 12px; padding: 2px 4px; cursor: pointer; }
  .gbox .member { padding: 6px 10px; flex-wrap: wrap; gap: 4px 8px; }
  .gbox .member label { flex: 1 1 110px; min-width: 110px; }
  .gbox .member .qwrap { margin-left: auto; }
  .gbox .member .group { width: 104px; margin: 0 8px 0 4px; }
  .gbox .gempty { padding: 8px 10px; font-size: 12px; color: #aaa; }
  .gbox .gother { display: block; margin-left: 24px; font-size: 11px; color: #06783b; }
  .gbox .gout { background: none; border: 1px solid #ddd; border-radius: 6px; color: #888; font-size: 12px; padding: 4px 10px; margin-left: auto; cursor: pointer; }
  .gadd { padding: 6px 10px 8px; }
  .sendsum { margin: 0 0 10px; padding: 8px 10px; background: #f6fbf8; border-radius: 8px; font-size: 14px; }
  .sendsum b { color: #06783b; font-size: 16px; }
  .sendsum span { display: block; font-size: 11px; color: #888; margin-top: 2px; }
  .gbox .member:has(input[type=checkbox]:not(:checked)) label { color: #aaa; }
  .gadd select { width: 100%; padding: 7px; border: 1px dashed #b6e2c6; border-radius: 8px; font-size: 13px; color: #06783b; background: #fff; }
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
  <div class="gnew">
    <input id="newGroup" type="text" placeholder="新しいグループ名（例: 火曜A班）" onkeydown="if(event.key==='Enter'&&!event.isComposing&&event.keyCode!==229){event.preventDefault();addGroup();}">
    <button class="sub" onclick="addGroup()">グループを作る</button>
  </div>
  <div class="muted" style="margin:-4px 0 10px">グループの見出しのチェックで、そのグループ全員をまとめて選択・解除できます。1人を複数のグループに入れることもできます（各グループの「＋ メンバーを追加」から）。コマ数はグループに入っていない人の個人レッスンの回数です。</div>
  <div id="members" class="muted">読み込み中...</div>
</div>

<h2>5. 送信</h2>
<div class="card">
  <button onclick="send()" id="sendBtn">この内容でLINEに送信する</button>
  <div id="result"></div>
  <div class="links" style="margin-top:14px">
    <a href="#" onclick="go('summarize');return false;">回答を集計する</a>
    <a href="../../../admin/reminders/__TENANT__">未回答・前日リマインド</a>
    <a href="#" onclick="runAssign();return false;">時間枠を自動で割り当てる</a>
    <a href="#" onclick="go('reset');return false;">回答をリセットする</a>
    <a href="#" onclick="go('keepalive');return false;">起動維持の状態</a>
  </div>
</div>

<h2 id="sec6">6. 確定した日程を各自に送る</h2>
<div class="card">
  <div id="savedNotice" style="display:none;background:#f0f9f3;border:1px solid #b6e2c6;border-radius:8px;padding:10px 12px;margin-bottom:12px;font-size:14px"></div>
  <div class="lbl">保存した割り当て</div>
  <div id="savedList" class="muted" style="margin-bottom:12px">読み込み中...</div>
  <div class="lbl">通知メッセージの前置き</div>
  <textarea id="notifyMessage" rows="2" oninput="saveNotifyMessage()"></textarea>
  <div class="row" style="margin-top:8px">
    <button class="sub" onclick="go('schedule')">割り当てを手動で調整する</button>
    <button id="notifyBtn" class="sub" onclick="goNotify()">送信内容を確認する（確定LINE）</button>
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

let MEMBERS = [];      // {name, quota, group, checked}
let GROUP_NAMES = [];  // 作成済みのグループ名（並び順どおり）
let _saveTimer = null;

function esc(t) {
  return String(t == null ? '' : t).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function allGroupNames() {
  const names = GROUP_NAMES.slice();
  MEMBERS.forEach(m => m.groups.forEach(g => { if (!names.includes(g)) names.push(g); }));
  return names;
}
function saveGroups() {
  clearTimeout(_saveTimer);
  _saveTimer = setTimeout(async () => {
    try {
      await fetch('/admin/groups.json?token=' + encodeURIComponent(TOKEN), {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          group_names: allGroupNames(),
          groups: MEMBERS.map(m => m.groups),
          quotas: MEMBERS.map(m => parseInt(m.quota, 10) || 0),
          send: MEMBERS.map(m => !!m.checked),
        }),
      });
    } catch (e) {}
  }, 400);
}
function toggleAll(on) {
  MEMBERS.forEach(m => m.checked = on);
  renderMembers(); saveGroups();
}
function inBox(m, key) { return key === '' ? !m.groups.length : m.groups.includes(key); }
function toggleGroup(name, on) {
  MEMBERS.forEach(m => { if (inBox(m, name)) m.checked = on; });
  renderMembers(); saveGroups();
}
function setChecked(i, on) { MEMBERS[i].checked = on; renderMembers(); saveGroups(); }
function joinGroup(name, i) {
  if (i === '' || i == null) return;
  const m = MEMBERS[+i];
  if (!m.groups.includes(name)) m.groups.push(name);
  renderMembers(); saveGroups();
}
function leaveGroup(i, name) {
  MEMBERS[i].groups = MEMBERS[i].groups.filter(g => g !== name);
  renderMembers(); saveGroups();
}
function setQuota(i, v) { MEMBERS[i].quota = v; saveGroups(); }
function addGroup() {
  const box = document.getElementById('newGroup');
  const name = box.value.replace(/[|,]/g, '').trim();
  if (!name) return;
  if (!allGroupNames().includes(name)) GROUP_NAMES.push(name);
  box.value = '';
  renderMembers(); saveGroups();
}
function removeGroup(name) {
  const n = MEMBERS.filter(m => m.groups.includes(name)).length;
  if (n && !confirm('グループ「' + name + '」をなくします（' + n + '人がこのグループから外れます）。よろしいですか？')) return;
  MEMBERS.forEach(m => { m.groups = m.groups.filter(g => g !== name); });
  GROUP_NAMES = GROUP_NAMES.filter(g => g !== name);
  renderMembers(); saveGroups();
}
function memberRow(m, i, key) {
  const others = m.groups.filter(g => g !== key);
  const also = others.length ? `<span class="gother">ほかに: ${others.map(esc).join('・')}</span>` : '';
  const right = key === ''
    ? `<span class="qwrap">コマ数 <input class="quota" type="number" min="0" max="9" value="${esc(m.quota)}" oninput="setQuota(${i}, this.value)"></span>`
    : `<button class="gout" onclick="leaveGroup(${i}, ${esc(JSON.stringify(key))})">外す</button>`;
  return `<div class="member">
     <label><input type="checkbox"${m.checked ? ' checked' : ''} onchange="setChecked(${i}, this.checked)"> ${i + 1}. ${esc(m.name)}${also}</label>
     ${right}
   </div>`;
}
function groupBox(title, key, solo) {
  const rows = [];
  MEMBERS.forEach((m, i) => { if (inBox(m, key)) rows.push(memberRow(m, i, key)); });
  const inGroup = MEMBERS.filter(m => inBox(m, key));
  const on = inGroup.filter(m => m.checked).length;
  const allOn = inGroup.length > 0 && on === inGroup.length;
  const keyArg = esc(JSON.stringify(key));
  let add = '';
  if (!solo) {
    const opts = MEMBERS.map((m, i) => m.groups.includes(key) ? '' : `<option value="${i}">${i + 1}. ${esc(m.name)}</option>`).join('');
    add = opts ? `<div class="gadd"><select onchange="joinGroup(${keyArg}, this.value)"><option value="">＋ メンバーを追加</option>${opts}</select></div>` : '';
  }
  return `<div class="gbox${solo ? ' solo' : ''}">
    <div class="ghead">
      <label><input type="checkbox" data-partial="${on > 0 && !allOn ? 1 : 0}"${allOn ? ' checked' : ''}${inGroup.length ? '' : ' disabled'}
        onchange="toggleGroup(${keyArg}, this.checked)"> ${esc(title)}
        <span class="gcount">${inGroup.length}人${inGroup.length ? '・選択 ' + on : ''}</span></label>
      ${solo ? '' : `<button class="gdel" onclick="removeGroup(${keyArg})">グループを削除</button>`}
    </div>
    ${rows.join('') || `<div class="gempty">${solo ? 'グループに入っていない人はいません。' : 'まだ誰もいません。下の「＋ メンバーを追加」から入れてください。'}</div>`}
    ${add}
  </div>`;
}
function renderMembers() {
  const box = document.getElementById('members');
  if (!MEMBERS.length) {
    box.className = 'muted';
    box.innerHTML = 'メンバーがまだ登録されていません。先に友だち追加してもらってください。';
    return;
  }
  const names = allGroupNames();
  const on = MEMBERS.filter(m => m.checked).length;
  box.className = '';
  box.innerHTML = `<div class="sendsum">送信する人: <b>${on}</b>人 / 全${MEMBERS.length}人<span>チェックの状態は自動で保存され、次回もそのまま残ります。新しく友だち追加した人は最初はチェックが入ります。</span></div>`
    + names.map(g => groupBox(g, g, false)).join('')
    + groupBox('個人（グループなし）', '', true);
  box.querySelectorAll('input[data-partial="1"]').forEach(cb => cb.indeterminate = true);
}
async function loadMembers() {
  try {
    const res = await fetch('/admin/members.json?token=' + encodeURIComponent(TOKEN));
    const data = await res.json();
    MEMBERS = (data.members || []).map(m => ({name: m.name, quota: m.quota,
      groups: (m.groups || (m.group ? [m.group] : [])).slice(), checked: m.send !== false}));
    GROUP_NAMES = (data.group_names || []).slice();
    renderMembers();
  } catch (e) {
    document.getElementById('members').textContent = 'メンバー取得に失敗しました: ' + e;
  }
}
async function send() {
  const list = candidates();
  const box = document.getElementById('result');
  if (!list.length) { box.className = 'err'; box.textContent = '候補が0件です。グリッドから時間を選んでください。'; return; }
  const to = MEMBERS.map((m, i) => m.checked ? String(i + 1) : null).filter(Boolean);
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
  const qs = MEMBERS.map(m => (m.quota === '' || m.quota == null) ? '1' : String(m.quota));
  if (!qs.length) { alert('メンバーがまだ登録されていません。'); return; }
  const gs = MEMBERS.map(m => m.groups.join(','));
  location.href = '/admin/assign?token=' + encodeURIComponent(TOKEN)
    + '&quotas=' + qs.join(',')
    + '&groups=' + encodeURIComponent(gs.join('|'));
}

async function loadSaved() {
  const box = document.getElementById('savedList');
  try {
    const res = await fetch('/admin/schedule.json?token=' + encodeURIComponent(TOKEN));
    const d = await res.json();
    const list = d.schedule || [];
    if (!list.length) { box.textContent = 'まだ割り当てがありません。「時間枠を自動で割り当てる」か「割り当てを手動で調整する」から作ってください。'; return; }
    const esc = t => String(t == null ? '' : t).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    let html = '<table style="width:100%;border-collapse:collapse;font-size:14px;color:#222">', last = '';
    list.forEach(s => {
      if (s.day !== last) { html += '<tr><td colspan="4" style="padding:8px 4px 4px;font-weight:600;color:#06783b">' + esc(s.day) + '</td></tr>'; last = s.day; }
      html += '<tr style="border-top:1px solid #f0f0f0"><td style="padding:5px 4px;white-space:nowrap">' + esc(s.time) + '〜' + esc(s.end) + '</td>'
        + '<td style="padding:5px 4px">' + esc(s.name) + (s.is_group ? ' <span style="font-size:11px;color:#888">グループ</span>' : '') + '</td>'
        + '<td style="padding:5px 4px;font-size:12px;color:#666">' + esc(s.location || '') + '</td>'
        + '<td style="padding:5px 4px;font-size:12px;color:#666">' + esc(s.fee || '') + '</td></tr>';
    });
    box.className = ''; box.innerHTML = html + '</table>';
  } catch (e) { box.textContent = '割り当ての読み込みに失敗しました: ' + e; }
}
function showSavedNotice() {
  const q = new URLSearchParams(location.search);
  if (!q.has('saved')) return;
  const n = document.getElementById('savedNotice');
  n.innerHTML = '<b>✓ 割り当てを保存しました（' + (parseInt(q.get('saved'), 10) || 0) + '件）</b><br>下の内容を確認して、「送信内容を確認する（確定LINE）」から生徒さんへ送ってください。'
    + (q.get('cal') === 'ng' ? '<br><span style="color:#b33">Googleカレンダーには反映できませんでした。講師ホームから再同期してください。</span>' : '');
  n.style.display = 'block';
  const btn = document.getElementById('notifyBtn');
  btn.className = ''; // 目立つ緑のボタンにする
  setTimeout(() => document.getElementById('sec6').scrollIntoView({ behavior: 'smooth' }), 300);
}

initMessage();
initNotify();
renderGrid();
renderPreview();
loadMembers();
loadSaved();
showSavedNotice();
</script>
</body>
</html>
"""


_background_lock = threading.Lock()
_last_background_start = None
BACKGROUND_INTERVAL_SECONDS = int(os.environ.get("BACKGROUND_INTERVAL_SECONDS", "600"))


def _run_background_jobs():
    """Run due reminders and backups. Only one run at a time."""
    if not _background_lock.acquire(blocking=False):
        return  # 前回の処理がまだ動いている
    try:
        try:
            from lesson_operations import run_due_automations

            run_due_automations(push_text_message)
        except Exception as exc:
            print(f"[healthz] automation error: {exc}", flush=True)
        try:
            from github_backup import run_due_backup

            run_due_backup()
        except Exception as exc:
            print(f"[healthz] backup error: {exc}", flush=True)
    finally:
        _background_lock.release()


@app.route("/healthz", methods=["GET", "HEAD"])
def healthz():
    """Keepalive endpoint. Answers immediately; reminders/backups run in the background
    so a slow LINE/Redis/GitHub call never makes Render's 5-second health check fail."""
    global _last_background_start
    if request.method == "GET" and not _background_lock.locked():
        now = time.monotonic()
        # Renderは数秒おきに確認するので、Redisの読み書き（無料枠）を節約するため
        # 自動送信・バックアップの確認は BACKGROUND_INTERVAL_SECONDS（既定10分）に1回だけ。
        if _last_background_start is None or now - _last_background_start >= BACKGROUND_INTERVAL_SECONDS:
            _last_background_start = now
            threading.Thread(target=_run_background_jobs, daemon=True).start()
    return "ok", 200


@app.route("/admin/keepalive", methods=["GET"])
def admin_keepalive():
    """起動維持(ping)の状態確認と、期限前に打ち切りたいときの手動停止。
    ?off=1 を付けると今すぐ止める。"""
    check_admin_token()
    import keepalive

    panel = admin_link("panel")

    if request.args.get("off"):
        msg = keepalive.disarm() or "cron-job.orgの設定（CRONJOB_API_KEY / CRONJOB_JOB_ID）がされていません。"
        return f'<pre>{msg}</pre><p><a href="{panel}">管理画面に戻る</a></p>'

    # ?arm=YYYY-MM-DD で、送信を伴わずに期限だけセットし直す（送信済みの期限を延ばしたいとき用）
    arm_date = request.args.get("arm")
    if arm_date is not None:
        msg = keepalive.arm(arm_date.strip()) or "cron-job.orgの設定（CRONJOB_API_KEY / CRONJOB_JOB_ID）がされていません。"
        back = admin_link("keepalive")
        return f'<pre>{msg}</pre><p><a href="{back}">状態を見る</a>　<a href="{panel}">管理画面に戻る</a></p>'

    s = keepalive.status()
    if not s.get("configured"):
        body = "cron-job.orgの設定（CRONJOB_API_KEY / CRONJOB_JOB_ID）がされていません。"
    elif s.get("error"):
        body = f"状態を取得できませんでした: {s['error']}"
    else:
        expires = s.get("expires_at") or 0
        if expires:
            e = str(expires)
            when = f"{e[0:4]}/{e[4:6]}/{e[6:8]} {e[8:10]}:{e[10:12]}"
        else:
            when = "設定なし（自動停止しません）"
        body = "\n".join([
            f"起動維持: {'ON（サーバーは寝ません）' if s['enabled'] else 'OFF'}",
            f"自動停止: {when}",
        ])

    off = admin_link("keepalive", off="1")
    return (
        f"<pre>{body}</pre>"
        f'<p><a href="{off}">今すぐ止める</a>　<a href="{panel}">管理画面に戻る</a></p>'
    )

@app.route("/admin/backup", methods=["GET"])
def admin_backup():
    """Upstashデータを外部（GitHub）へバックアップする仕組みの状態確認。
    ?run=1 を付けると今すぐ1回実行する。"""
    check_admin_token()
    import github_backup

    panel = admin_link("panel")

    if request.args.get("run"):
        result = github_backup.run_backup(reason="manual")
        back = admin_link("backup")
        return f'<pre>{result}</pre><p><a href="{back}">状態を見る</a>　<a href="{panel}">管理画面に戻る</a></p>'

    s = github_backup.status()
    if not s.get("configured"):
        body = "GitHubバックアップの設定（GITHUB_BACKUP_TOKEN / GITHUB_BACKUP_REPO）がされていません。"
    else:
        last = s.get("last_run") or {}
        if last:
            state = "成功" if last.get("ok") else f"失敗（{last.get('error', '')}）"
            detail = f"{last.get('date', '?')}: {state}"
        else:
            detail = "まだ実行されていません"
        body = "\n".join([
            f"バックアップ先: {s['repo']}",
            f"直近の実行: {detail}",
        ])

    run_now = admin_link("backup", run="1")
    return (
        f"<pre>{body}</pre>"
        f'<p><a href="{run_now}">今すぐバックアップする</a>　<a href="{panel}">管理画面に戻る</a></p>'
    )

@app.route("/admin/panel", methods=["GET"])
def admin_panel():
    """管理者用の入力フォーム。日付と時間帯を選ぶだけで候補リストを組み立てて送信できる。"""
    check_admin_token()
    return (
        ADMIN_PANEL_HTML.replace("__PANEL_NAME__", str(PANEL_NAME))
        .replace("__TENANT__", get_tenant().name)
    )


@app.route("/admin/groups.json", methods=["POST"])
def admin_groups_save():
    """管理画面の送信先で作ったグループ・各自のグループ・コマ数を保存する。
    groups / quotas はメンバー一覧の並び順に対応するリスト。"""
    check_admin_token()
    data = request.get_json(silent=True) or {}
    members = load_json("members")
    group_list = data.get("groups") or []
    quota_list = data.get("quotas") or []
    send_list = data.get("send")

    def clean(name):
        return str(name or "").replace("|", "").replace(",", "").strip()[:40]

    def clean_list(value):
        items = value if isinstance(value, (list, tuple)) else [value]
        out = []
        for item in items:
            name = clean(item)
            if name and name not in out:
                out.append(name)
        return out

    groups = load_json("groups", default={})
    quotas = load_json("quotas", default={})
    for i, mem in enumerate(members):
        if i < len(group_list):
            groups[mem["user_id"]] = clean_list(group_list[i])
        if i < len(quota_list):
            try:
                quotas[mem["user_id"]] = max(0, min(9, int(quota_list[i])))
            except (TypeError, ValueError):
                pass
    names = []
    for name in data.get("group_names") or []:
        name = clean(name)
        if name and name not in names:
            names.append(name)
    if isinstance(send_list, list):
        # チェックを外した人（送らない人）だけを覚える。新しい友だちは自動で「送る」になる。
        send_off = [
            mem["user_id"] for i, mem in enumerate(members)
            if i < len(send_list) and send_list[i] is False
        ]
        save_json("send_off", send_off)
    save_json("groups", groups)
    save_json("quotas", quotas)
    save_json("group_names", names)
    return {"ok": True, "group_names": names}


def assign_group_list(value):
    from assign import group_list
    return group_list(value)


@app.route("/admin/members.json", methods=["GET"])
def admin_members_json():
    """管理画面が送信先とコマ数を描画するためのメンバー一覧"""
    check_admin_token()
    members = load_json("members")
    quotas = load_json("quotas", default={})
    groups = load_json("groups", default={})
    send_off = set(load_json("send_off", default=[]) or [])
    return {
        "group_names": load_json("group_names", default=[]),
        "members": [
            {
                "name": m["display_name"],
                "quota": int(quotas.get(m["user_id"], 1)),
                "groups": assign_group_list(groups.get(m["user_id"], "")),
                "send": m["user_id"] not in send_off,
            }
            for m in members
        ]
    }


@app.route("/admin/assign", methods=["GET"])
def admin_assign():
    """?token=...&quotas=1,2,1&groups=|A班|A班 の形式で、回答をもとに時間枠を自動割り当てする。
    quotas（カンマ区切り）とgroups（縦棒区切り）はメンバー一覧の並び順に対応する。
    1人が複数のグループに入るときは groups=A班,B班|... のようにカンマで区切る。
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
        groups[mem["user_id"]] = (
            assign_group_list(group_names[i].split(",")) if i < len(group_names) else []
        )
    save_json("quotas", quotas)
    save_json("groups", groups)

    candidates = load_json("candidates", default=[])
    votes = load_json("votes")
    locations = {l["user_id"]: l["location"] for l in load_json("locations")}
    from lesson_operations import other_tenant_busy

    day_labels = list(dict.fromkeys(assign_mod.split_slot(c)[0] for c in candidates))
    busy = other_tenant_busy(get_tenant().name, day_labels)
    result = assign_mod.auto_assign(
        candidates, votes, quotas, locations, groups,
        busy=busy, all_online=get_tenant().name == "online",
    )

    # 同じ講師が関西・関東で同時刻に入らないよう、地域をまたいで検算する。
    from lesson_operations import cross_tenant_conflicts

    conflicts = cross_tenant_conflicts(get_tenant().name, result.get("schedule", []))
    if conflicts:
        panel = admin_link("panel")
        detail = "\n".join(f"・{line}" for line in conflicts)
        return (
            f"<pre>地域をまたぐ日程重複があるため保存しませんでした。\n\n{detail}</pre>"
            f'<p><a href="{panel}">管理画面に戻る</a></p>',
            409,
        )

    # 通知で使えるように結果を保存し、Googleカレンダーにも反映する。
    save_json("assignment", result.get("schedule", []))
    from lesson_operations import sync_calendar_schedule

    calendar_result = sync_calendar_schedule(get_tenant().name)

    panel = admin_link("panel")
    body = assign_mod.format_result(result)
    if calendar_result.get("configured"):
        if calendar_result.get("ok"):
            body += (
                "\n\nGoogleカレンダー同期済み "
                f"（追加{calendar_result['created']}・更新{calendar_result['updated']}・削除{calendar_result['deleted']}）"
            )
        else:
            body += "\n\nGoogleカレンダーは未反映です。講師ホームから再同期してください。"
    notify = admin_link("notify")
    edit = admin_link("schedule")
    links = (
        f'<p><a href="{edit}">担当を手動で調整する</a>　'
        f'<a href="{notify}">この内容を各自にLINEで送る</a>　'
        f'<a href="{panel}">管理画面に戻る</a></p>'
    )
    return f"<pre>{body}</pre>{links}"


SCHEDULE_EDIT_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>割り当ての調整</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans", sans-serif; max-width: 860px; margin: 0 auto; padding: 16px 16px 90px; color: #222; background: #fafafa; }
  h1 { font-size: 20px; margin: 4px 0 12px; }
  h2 { font-size: 16px; margin: 0 0 8px; display: flex; align-items: center; gap: 8px; }
  .card { background: #fff; border: 1px solid #e2e2e2; border-radius: 10px; padding: 14px; margin-bottom: 14px; }
  .muted { color: #888; font-size: 12px; }
  .toggle { margin-left: auto; font-size: 12px; background: none; border: 1px solid #ddd; color: #555; padding: 4px 10px; border-radius: 14px; }
  .bday { font-weight: 700; color: #06783b; margin: 12px 0 4px; font-size: 14px; }
  .brow { display: flex; gap: 8px; align-items: flex-start; padding: 5px 0; border-top: 1px solid #f1f1f1; }
  .brow .bt { width: 48px; flex: 0 0 48px; font-size: 13px; padding-top: 4px; color: #555; }
  .brow .bp { display: flex; flex-wrap: wrap; gap: 5px; flex: 1; }
  .pchip { border: 1px solid #cfd6da; background: #fff; border-radius: 14px; padding: 4px 9px; font-size: 13px; cursor: pointer; color: #333; }
  .pchip small { color: #888; margin-left: 3px; font-size: 10px; }
  .pchip.done { background: #e8f4ef; border-color: #b6e2c6; color: #0f6e56; }
  .pchip.done::before { content: "✓ "; }
  .busy { font-size: 11px; color: #b06000; background: #fff3dd; border-radius: 10px; padding: 3px 8px; }
  .none { font-size: 12px; color: #bbb; padding-top: 4px; }
  .list2 { font-size: 13px; line-height: 1.7; }
  .list2 b { display: inline-block; min-width: 92px; color: #555; }
  .lday { font-weight: 700; color: #06783b; background: #f6fbf8; padding: 7px 10px; border-radius: 6px; margin: 14px 0 6px; display: flex; align-items: center; }
  .lday button { margin-left: auto; }
  .lrow { border: 1px solid #e6e6e6; border-radius: 9px; padding: 9px; margin-bottom: 8px; background: #fff; }
  .lrow.warn { border-color: #f0b400; background: #fffaf0; }
  .lrow .r { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .lrow .r + .r { margin-top: 6px; }
  .lrow input, .lrow select { padding: 7px 8px; border: 1px solid #ccc; border-radius: 6px; font-size: 14px; background: #fff; }
  .lrow input.day { width: 108px; }
  .lrow input[type=time] { width: 104px; }
  .lrow select.who { flex: 1; min-width: 140px; }
  .lrow select.loc { min-width: 120px; }
  .lrow input.fee { flex: 1; min-width: 180px; font-size: 13px; }
  .len { font-size: 12px; padding: 6px 8px; border: 1px solid #b6e2c6; background: #fff; color: #06783b; border-radius: 6px; cursor: pointer; }
  .del { margin-left: auto; font-size: 12px; padding: 6px 10px; border: 1px solid #e0b4b4; background: #fff; color: #b33; border-radius: 6px; cursor: pointer; }
  .wtxt { font-size: 12px; color: #a06000; width: 100%; }
  button { padding: 10px 16px; font-size: 14px; border: none; border-radius: 6px; cursor: pointer; background: #06C755; color: #fff; }
  button.sub { background: #eee; color: #333; }
  button.small { padding: 5px 10px; font-size: 12px; background: #fff; color: #06783b; border: 1px solid #b6e2c6; }
  #warns { font-size: 13px; }
  #warns li { margin: 3px 0; color: #8a5900; }
  .bar { position: fixed; left: 0; right: 0; bottom: 0; background: #fff; border-top: 1px solid #ddd; padding: 10px 16px; display: flex; gap: 8px; justify-content: center; z-index: 5; }
  #msg { margin-top: 10px; padding: 10px; border-radius: 8px; font-size: 13px; display: none; }
  #msg.ok { background: #f0f9f3; border: 1px solid #b6e2c6; display: block; }
  #msg.err { background: #fdf2f2; border: 1px solid #e0b4b4; display: block; }
  /* パソコンでは左にレッスン一覧、右に集計表 */
  @media (min-width: 1000px) {
    body { max-width: 1320px; }
    .cols { display: grid; grid-template-columns: minmax(0, 1fr) 420px; gap: 16px; align-items: start; }
    .leftcol { grid-column: 1; grid-row: 1; }
    .boardcard { grid-column: 2; grid-row: 1; position: sticky; top: 12px; max-height: calc(100vh - 96px); overflow: auto; }
  }
</style>
</head>
<body>
<h1>割り当ての調整</h1>

<div class="cols">
<div class="card boardcard">
  <h2>集計表 <span class="muted">名前を押すと、その時間にレッスンを追加します</span><button class="toggle" onclick="toggleBoard()">表示/非表示</button></h2>
  <div id="board"></div>
</div>

<div class="leftcol">
<div class="card">
  <h2>レッスン一覧</h2>
  <div class="muted">日付・時間・生徒・教室・料金を自由に変更できます。「料金」は確定のお知らせに載ります（空欄なら載りません）。</div>
  <div id="list"></div>
  <div style="margin-top:10px"><button class="small" onclick="addRow(lastDay())">＋ レッスンを追加</button></div>
</div>

<div class="card" id="warnCard" style="display:none">
  <h2>確認してほしいこと</h2>
  <ul id="warns"></ul>
</div>
<div id="msg"></div>
</div>
</div>

<div class="bar">
  <button onclick="save()">この内容で保存する</button>
  <button class="sub" onclick="location.href='/admin/panel?token=' + encodeURIComponent(TOKEN)">戻る</button>
</div>

<script>
const TOKEN = new URLSearchParams(location.search).get('token') || '';
let data = null, rows = [], boardOpen = true;

function esc(t) { return String(t == null ? '' : t).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function toMin(t) { const p = String(t || '').split(':'); return p.length === 2 ? parseInt(p[0], 10) * 60 + parseInt(p[1], 10) : NaN; }
function toTime(m) { return Math.floor(m / 60) + ':' + String(m % 60).padStart(2, '0'); }
function pad(t) { const m = toMin(t); return isNaN(m) ? '' : String(Math.floor(m / 60)).padStart(2, '0') + ':' + String(m % 60).padStart(2, '0'); }
function ent(name) { return (data.entities || []).find(e => e.name === name); }
function dayOrder(d) { const p = String(d || '').split('/'); const mo = parseInt(p[0], 10), da = parseInt(p[1] || '', 10); return (isNaN(mo) || isNaN(da)) ? 9999 : mo * 100 + da; }
function allDays() {
  const s = new Set((data.board.days || []).concat(rows.map(r => r.day)).filter(Boolean));
  return [...s].sort((a, b) => dayOrder(a) - dayOrder(b));
}
function lastDay() { const d = allDays(); return d.length ? d[0] : ''; }
function personInRows(name) {
  return rows.some(r => r.name === name || (ent(r.name) && ent(r.name).is_group && (data.groupMembers[r.name] || []).includes(name)));
}

function renderBoard() {
  const box = document.getElementById('board');
  if (!boardOpen) { box.innerHTML = '<p class="muted">非表示中</p>'; return; }
  const b = data.board;
  if (!b.slots.length) { box.innerHTML = '<p class="muted">日程の候補がありません。</p>'; return; }
  let html = '';
  b.days.forEach(day => {
    html += '<div class="bday">' + esc(day) + '</div>';
    const busy = (b.busy[day] || []).map(x => '<span class="busy">他の地域: ' + toTime(x[0] * 30) + '〜' + toTime(x[1] * 30) + (x[2] ? '（オンライン）' : '（対面）') + '</span>').join(' ');
    if (busy) html += '<div style="margin:2px 0 4px">' + busy + '</div>';
    b.slots.filter(s => s.day === day).forEach(s => {
      const chips = s.people.map(p => {
        const done = personInRows(p.name) ? ' done' : '';
        return '<button class="pchip' + done + '" onclick="addFromBoard(' + esc(JSON.stringify(day)) + ',' + esc(JSON.stringify(s.time)) + ',' + esc(JSON.stringify(p.name)) + ',' + esc(JSON.stringify(p.location || '')) + ')">'
          + esc(p.name) + (p.location ? '<small>' + esc(p.location.replace('教室', '')) + '</small>' : '') + '</button>';
      }).join('');
      html += '<div class="brow"><span class="bt">' + esc(s.time) + '</span><div class="bp">' + (chips || '<span class="none">なし</span>') + '</div></div>';
    });
  });
  html += '<div class="list2" style="margin-top:12px">'
    + '<div><b>参加できない</b>' + (b.skips.length ? esc(b.skips.join('、')) : '—') + '</div>'
    + '<div><b>未回答</b>' + (b.nonrespondents.length ? esc(b.nonrespondents.join('、')) : '—') + '</div>'
    + (b.comments.length ? '<div><b>コメント</b></div>' + b.comments.map(c => '<div style="margin-left:12px">・' + esc(c.name) + '：' + esc(c.text) + '</div>').join('') : '')
    + '</div>';
  box.innerHTML = html;
}
function toggleBoard() { boardOpen = !boardOpen; renderBoard(); }

function locOptions(cur) {
  const list = [''].concat(data.locations || []);
  if (cur && !list.includes(cur)) list.push(cur);
  return list.map(l => '<option value="' + esc(l) + '"' + (l === cur ? ' selected' : '') + '>' + (l ? esc(l) : '（教室なし）') + '</option>').join('');
}
function whoOptions(cur) {
  const list = [''].concat(data.names);
  if (cur && !list.includes(cur)) list.push(cur);
  return list.map(n => {
    const e = ent(n);
    const label = n ? n + (e && e.is_group ? '（グループ' + e.size + '人）' : '') : '（未選択）';
    return '<option value="' + esc(n) + '"' + (n === cur ? ' selected' : '') + '>' + esc(label) + '</option>';
  }).join('');
}

function renderList() {
  const box = document.getElementById('list');
  const warnings = computeWarnings();
  if (!rows.length) { box.innerHTML = '<p class="muted">まだレッスンがありません。上の集計表の名前を押すか、「＋ レッスンを追加」で入れてください。</p>'; renderWarns(warnings); return; }
  const order = rows.map((r, i) => i).sort((a, b) => (dayOrder(rows[a].day) - dayOrder(rows[b].day)) || (toMin(rows[a].time) - toMin(rows[b].time)));
  let html = '', last = null;
  order.forEach(i => {
    const r = rows[i];
    if (r.day !== last) {
      html += '<div class="lday">' + esc(r.day || '（日付未入力）') + '<button class="small" onclick="addRow(' + esc(JSON.stringify(r.day)) + ')">＋ この日に追加</button></div>';
      last = r.day;
    }
    const w = warnings.byRow[i] || [];
    const mins = toMin(r.end) - toMin(r.time);
    html += '<div class="lrow' + (w.length ? ' warn' : '') + '">'
      + '<div class="r">'
      + '<input class="day" list="dayList" value="' + esc(r.day) + '" onchange="setDay(' + i + ',this.value)">'
      + '<input type="time" step="900" value="' + pad(r.time) + '" onchange="setStart(' + i + ',this.value)">'
      + '〜<input type="time" step="900" value="' + pad(r.end) + '" onchange="setEnd(' + i + ',this.value)">'
      + '<span class="muted">' + (isNaN(mins) ? '' : mins + '分') + '</span>'
      + [60, 90, 120].map(m => '<button class="len" onclick="setLen(' + i + ',' + m + ')">' + m + '分</button>').join('')
      + '</div><div class="r">'
      + '<select class="who" onchange="setWho(' + i + ',this.value)">' + whoOptions(r.name) + '</select>'
      + '<select class="loc" onchange="setLoc(' + i + ',this.value)">' + locOptions(r.location) + '</select>'
      + '</div><div class="r">'
      + '<input class="fee" maxlength="200" value="' + esc(r.fee || '') + '" oninput="rows[' + i + '].fee=this.value" placeholder="料金（例：レッスン料 4,500円＋部屋代 459円＝4,959円）">'
      + '<button class="del" onclick="delRow(' + i + ')">削除</button>'
      + (w.length ? '<div class="wtxt">⚠ ' + esc(w.join(' / ')) + '</div>' : '')
      + '</div></div>';
  });
  html += '<datalist id="dayList">' + allDays().map(d => '<option value="' + esc(d) + '">').join('') + '</datalist>';
  box.innerHTML = html;
  renderWarns(warnings);
}
function renderWarns(w) {
  const card = document.getElementById('warnCard');
  card.style.display = w.all.length ? 'block' : 'none';
  document.getElementById('warns').innerHTML = w.all.map(t => '<li>' + esc(t) + '</li>').join('');
}

function computeWarnings() {
  const byRow = {}, all = [];
  const add = (i, t) => { (byRow[i] = byRow[i] || []).push(t); };
  rows.forEach((r, i) => {
    if (!r.name) add(i, '生徒が未選択（保存されません）');
    if (isNaN(toMin(r.time)) || isNaN(toMin(r.end)) || toMin(r.end) <= toMin(r.time)) add(i, '時間を確認してください');
  });
  const isOnline = r => r.location === 'オンライン' || data.all_online;
  for (let a = 0; a < rows.length; a++) for (let b = a + 1; b < rows.length; b++) {
    const A = rows[a], B = rows[b];
    if (!A.day || A.day !== B.day) continue;
    const a1 = toMin(A.time), a2 = toMin(A.end), b1 = toMin(B.time), b2 = toMin(B.end);
    if (a1 < b2 && b1 < a2) { add(a, '他のレッスンと時間が重なっています'); add(b, '他のレッスンと時間が重なっています'); continue; }
    const gap = a2 <= b1 ? b1 - a2 : a1 - b2;
    const placeA = isOnline(A) ? 'オンライン' : (A.location || '').replace('（どちらでもOK）', '');
    const placeB = isOnline(B) ? 'オンライン' : (B.location || '').replace('（どちらでもOK）', '');
    if (placeA && placeB && placeA !== placeB && gap < 60) {
      const t = '場所が変わるのに間が' + gap + '分しかありません（' + placeA + '→' + placeB + '）';
      add(a, t); add(b, t);
    }
  }
  rows.forEach((r, i) => {
    (data.board.busy[r.day] || []).forEach(x => {
      const s = x[0] * 30, e = x[1] * 30, r1 = toMin(r.time), r2 = toMin(r.end);
      if (r1 < e && s < r2) add(i, '他の地域の予定と重なっています（保存できません）');
      else if ((x[2] !== isOnline(r)) && (Math.min(Math.abs(r1 - e), Math.abs(s - r2)) < 60)) add(i, '他の地域の予定との間が1時間未満です');
    });
  });
  const counts = {};
  rows.forEach(r => { if (r.name) counts[r.name] = (counts[r.name] || 0) + 1; });
  Object.keys(counts).forEach(n => { if (counts[n] > 1) all.push(n + 'さんが' + counts[n] + '回入っています（意図どおりならOK）'); });
  const voted = new Set();
  data.board.slots.forEach(s => s.people.forEach(p => voted.add(p.name)));
  const missing = [...voted].filter(n => !personInRows(n));
  if (missing.length) all.push('回答したのにレッスンが入っていない人: ' + missing.join('、'));
  Object.keys(byRow).forEach(i => byRow[i] = [...new Set(byRow[i])]);
  const rowWarn = Object.values(byRow).flat();
  [...new Set(rowWarn)].forEach(t => all.unshift(t));
  return { byRow, all: [...new Set(all)] };
}

function refresh() { renderList(); renderBoard(); }
function setDay(i, v) { rows[i].day = v.trim(); refresh(); }
function setEnd(i, v) { rows[i].end = v; refresh(); }
function setLoc(i, v) { rows[i].location = v; refresh(); }
function setStart(i, val) {
  const r = rows[i], len = toMin(r.end) - toMin(r.time);
  r.time = val;
  if (!isNaN(len) && len > 0 && !isNaN(toMin(val))) r.end = toTime(toMin(val) + len);
  refresh();
}
function setLen(i, m) { const r = rows[i]; if (!isNaN(toMin(r.time))) { r.end = toTime(toMin(r.time) + m); refresh(); } }
function setWho(i, name) {
  const r = rows[i], e = ent(name);
  r.name = name;
  if (e && e.location && !r.location) r.location = e.location;
  if (e && r.fresh && !isNaN(toMin(r.time))) r.end = toTime(toMin(r.time) + e.minutes);
  refresh();
}
function delRow(i) { rows.splice(i, 1); refresh(); }
function addRow(day, time = '', name = '', loc = '') {
  const e = ent(name);
  const start = time || '10:00';
  const minutes = e ? e.minutes : 60;
  rows.push({ src: null, day: day || '', time: start, end: toTime(toMin(start) + minutes), name: name || '',
              location: loc || (e && e.location) || '', fee: '', fresh: !name });
  refresh();
}
function addFromBoard(day, time, person, loc) {
  // その人が入っているグループがあれば選べるように（初期は個人）
  addRow(day, time, person, (loc && loc !== 'どちらでもOK') ? loc : '');
  document.getElementById('list').lastElementChild && window.scrollTo({ top: document.getElementById('list').getBoundingClientRect().top + window.scrollY - 60, behavior: 'smooth' });
}

async function save() {
  const blocked = rows.some((r, i) => (computeWarnings().byRow[i] || []).some(t => t.indexOf('時間を確認') >= 0));
  if (blocked) { alert('時間が正しくないレッスンがあります。直してから保存してください。'); return; }
  const w = computeWarnings();
  if (w.all.length && !confirm('確認してほしいことが' + w.all.length + '件あります。このまま保存しますか？')) return;
  const box = document.getElementById('msg');
  box.className = ''; box.style.display = 'none';
  try {
    const res = await fetch('/admin/schedule/save?token=' + encodeURIComponent(TOKEN), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rows: rows.filter(r => r.name).map(r => ({ src: r.src, day: r.day, time: r.time, end: r.end, name: r.name, location: r.location, fee: (r.fee || '').trim() })) }),
    });
    const r = await res.json();
    box.className = res.ok ? 'ok' : 'err';
    let note = '';
    if (res.ok && r.calendar && r.calendar.configured) note = r.calendar.ok ? ' Googleカレンダーにも同期しました。' : ' Googleカレンダーは未反映です。講師ホームから再同期してください。';
    box.textContent = res.ok ? ('保存しました。確定のお知らせにはこの内容が使われます。' + note) : ('エラー: ' + (r.error || ''));
    if (res.ok) {
      const n = (r.schedule || []).length;
      location.href = '/admin/panel?token=' + encodeURIComponent(TOKEN) + '&saved=' + n + (r.calendar && r.calendar.configured && !r.calendar.ok ? '&cal=ng' : '');
      return;
    }
    box.scrollIntoView({ behavior: 'smooth' });
  } catch (e) { box.className = 'err'; box.textContent = 'エラー: ' + e; }
}
async function load() {
  const res = await fetch('/admin/schedule.json?token=' + encodeURIComponent(TOKEN));
  data = await res.json();
  data.groupMembers = data.groupMembers || {};
  rows = (data.schedule || []).map((s, i) => ({ src: i, day: s.day, time: s.time, end: s.end, name: s.name || '', location: s.location || '', fee: s.fee || '' }));
  refresh();
}
load();
</script>
</body>
</html>
"""


def _entity_directory():
    """名前 → (member_ids, グループかどうか, 教室) の対応表を作る"""
    import assign as assign_mod

    candidates = load_json("candidates", default=[])
    votes = load_json("votes")
    quotas = load_json("quotas", default={})
    groups = load_json("groups", default={})
    locations = {l["user_id"]: l["location"] for l in load_json("locations")}

    avail, names = assign_mod.build_availability(candidates, votes)
    entities = assign_mod.build_entities(avail, names, quotas, locations, groups)

    directory = {
        e["name"]: {
            "member_ids": list(e["member_ids"]),
            "is_group": e["is_group"],
            "location": e["location"] if e["location"] != assign_mod.ANY_LOCATION else "",
        }
        for e in entities.values()
    }
    # 回答していないグループも選べるようにする
    members_by_id = {m["user_id"]: m for m in load_json("members")}
    for user_id, value in groups.items():
        for gname in assign_mod.group_list(value):
            if user_id in members_by_id:
                row = directory.setdefault(gname, {"member_ids": [], "is_group": True, "location": ""})
                if row.get("is_group") and user_id not in row["member_ids"]:
                    row["member_ids"].append(user_id)
    # 回答していない人も手動で入れられるようにしておく
    for m in load_json("members"):
        directory.setdefault(
            m["display_name"],
            {"member_ids": [m["user_id"]], "is_group": False, "location": ""},
        )
    return directory


@app.route("/admin/schedule", methods=["GET"])
def admin_schedule():
    """割り当て結果を手動で調整する画面"""
    check_admin_token()
    return SCHEDULE_EDIT_HTML


def default_lesson_minutes(is_group: bool, size: int) -> int:
    """料金表どおりの長さ：個人60分、2人組120分、3人以上90分"""
    if not is_group:
        return 60
    return 120 if size <= 2 else 90


def _schedule_board():
    """調整画面の上に出す集計表（誰がどの時間に○を付けたか）"""
    import assign as assign_mod
    from lesson_operations import other_tenant_busy

    candidates = load_json("candidates", default=[])
    votes = load_json("votes")
    members = load_json("members")
    names = {m["user_id"]: m["display_name"] for m in members}
    locations = {l["user_id"]: l["location"] for l in load_json("locations")}
    comments = {c["user_id"]: c.get("text", "") for c in load_json("comments")}
    skips = [str(u) for u in load_json("skips", default=[]) or []]
    targets = load_json("schedule_targets", default=[]) or []

    slots = []
    for i, label in enumerate(candidates, start=1):
        day, time_ = assign_mod.split_slot(label)
        people = []
        for v in votes:
            if v.get("candidate_index") == i:
                uid = v.get("user_id", "")
                people.append({"name": names.get(uid, v.get("display_name", uid)), "location": locations.get(uid, "")})
        slots.append({"day": day, "time": time_, "people": people})

    voted = {v.get("user_id") for v in votes}
    pool = [m for m in members if not targets or m["user_id"] in targets]
    days = list(dict.fromkeys(s["day"] for s in slots))
    return {
        "slots": slots,
        "days": days,
        "skips": [names.get(u, u) for u in skips],
        "nonrespondents": [m["display_name"] for m in pool if m["user_id"] not in voted and m["user_id"] not in skips],
        "comments": [{"name": names.get(u, u), "text": t} for u, t in comments.items() if t.strip()],
        "busy": other_tenant_busy(get_tenant().name, days),
    }


@app.route("/admin/schedule.json", methods=["GET"])
def admin_schedule_json():
    """調整画面が読み込む、現在の割り当てと選択肢"""
    check_admin_token()
    directory = _entity_directory()
    names_by_id = {m["user_id"]: m["display_name"] for m in load_json("members")}
    return {
        "schedule": load_json("assignment", default=[]),
        "names": sorted(directory.keys()),
        "entities": [
            {
                "name": name,
                "is_group": info["is_group"],
                "size": len(info["member_ids"]),
                "location": info.get("location", ""),
                "minutes": default_lesson_minutes(info["is_group"], len(info["member_ids"])),
            }
            for name, info in sorted(directory.items())
        ],
        "locations": [l for l in LOCATIONS if l != "どちらでもOK"],
        "board": _schedule_board(),
        "groupMembers": {
            name: [names_by_id.get(u, u) for u in info["member_ids"]]
            for name, info in directory.items() if info["is_group"]
        },
        "all_online": get_tenant().name == "online",
    }


@app.route("/admin/schedule/save", methods=["POST"])
def admin_schedule_save():
    """調整した割り当てを保存する。空欄にした枠は削除される。"""
    check_admin_token()
    body = request.get_json(silent=True) or {}
    if "rows" in body:
        return _save_schedule_rows(body.get("rows") or [])
    names = body.get("names", [])
    fees = body.get("fees")

    schedule = load_json("assignment", default=[])
    if len(names) != len(schedule) or (fees is not None and len(fees) != len(schedule)):
        return {"error": "画面が古くなっています。読み込み直してください。"}, 400

    directory = _entity_directory()
    updated = []
    for index, (slot, name) in enumerate(zip(schedule, names)):
        name = (name or "").strip()
        if not name:
            continue  # 空きにした枠は削除
        info = directory.get(name)
        if not info:
            return {"error": f"「{name}」が見つかりません。"}, 400
        slot = dict(slot)
        slot["name"] = name
        slot["member_ids"] = info["member_ids"]
        slot["is_group"] = info["is_group"]
        slot["location"] = info["location"] or slot.get("location", "")
        slot["absent"] = []
        slot["manual"] = True
        if fees is not None:
            fee = str(fees[index] or "").strip()[:200]
            if fee:
                slot["fee"] = fee
            else:
                slot.pop("fee", None)
        updated.append(slot)

    from lesson_operations import cross_tenant_conflicts

    conflicts = cross_tenant_conflicts(get_tenant().name, updated)
    if conflicts:
        return {"error": "地域をまたぐ日程重複があります。 " + " ".join(conflicts)}, 409
    save_json("assignment", updated)
    from lesson_operations import sync_calendar_schedule

    calendar_result = sync_calendar_schedule(get_tenant().name)
    return {
        "schedule": updated,
        "names": sorted(directory.keys()),
        "calendar": calendar_result,
    }


def _save_schedule_rows(rows: list):
    """自由に編集した一覧（日付・開始・終了・生徒・教室・料金）を保存する"""
    import re as re_mod

    original = load_json("assignment", default=[])
    directory = _entity_directory()
    updated = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        info = directory.get(name)
        if not info:
            return {"error": f"「{name}」が見つかりません。"}, 400
        day = str(row.get("day") or "").strip()
        start = str(row.get("time") or "").strip()
        end = str(row.get("end") or "").strip()
        if not day or not re_mod.fullmatch(r"\d{1,2}:\d{2}", start) or not re_mod.fullmatch(r"\d{1,2}:\d{2}", end):
            return {"error": f"「{name}」の日付か時刻が正しくありません。"}, 400
        sh, sm = map(int, start.split(":"))
        eh, em = map(int, end.split(":"))
        if eh * 60 + em <= sh * 60 + sm:
            return {"error": f"「{name}」の終了時刻が開始より前になっています。"}, 400
        src = row.get("src")
        slot = dict(original[src]) if isinstance(src, int) and 0 <= src < len(original) else {}
        changed_time = (slot.get("day"), slot.get("time"), slot.get("end")) != (day, f"{sh}:{sm:02d}", f"{eh}:{em:02d}")
        if changed_time or slot.get("name") != name:
            slot.pop("lesson_id", None)  # 別のレッスンとして扱う
        slot.update(
            day=day,
            time=f"{sh}:{sm:02d}",
            end=f"{eh}:{em:02d}",
            name=name,
            member_ids=list(info["member_ids"]),
            is_group=info["is_group"],
            location=str(row.get("location") or "").strip() or info.get("location", ""),
            absent=[],
            manual=True,
            start_unit=sh * 2 + (1 if sm >= 30 else 0),
            length=max(1, ((eh * 60 + em) - (sh * 60 + sm) + 29) // 30),
        )
        fee = str(row.get("fee") or "").strip()[:200]
        if fee:
            slot["fee"] = fee
        else:
            slot.pop("fee", None)
        updated.append(slot)

    from lesson_operations import cross_tenant_conflicts, parse_schedule_date

    def sort_key(s):
        d = parse_schedule_date(s.get("day", ""))
        h, m = map(int, s["time"].split(":"))
        return (d.isoformat() if d else s.get("day", ""), h * 60 + m)

    updated.sort(key=sort_key)
    conflicts = cross_tenant_conflicts(get_tenant().name, updated)
    if conflicts:
        return {"error": "地域をまたぐ日程重複があります。 " + " ".join(conflicts)}, 409
    save_json("assignment", updated)
    from lesson_operations import sync_calendar_schedule

    calendar_result = sync_calendar_schedule(get_tenant().name)
    return {"ok": True, "schedule": updated, "calendar": calendar_result}


@app.route("/admin/notify", methods=["GET"])
def admin_notify():
    """割り当て結果を各自にLINEで通知する。
    確認なしで送らないよう、まずプレビューを表示し &send=1 で実際に送信する。"""
    check_admin_token()
    from schedule_tools import (
        build_notifications, send_notifications, DEFAULT_NOTIFY_MESSAGE,
        build_unassigned_notification, DEFAULT_SORRY_MESSAGE,
    )
    import html as html_lib

    schedule = load_json("assignment", default=[])
    if not schedule:
        return (
            "<pre>送信できる割り当て結果がありません。先に「時間枠を自動で割り当てる」を実行してください。</pre>"
            f'<p><a href="{admin_link("panel")}">管理画面に戻る</a></p>'
        )

    message = request.args.get("message", "") or load_json("notify_message", default="")
    save_json("notify_message", message)
    items = build_notifications(schedule, message)

    # 枠に入らなかった人へのお知らせ（毎回チェックで選ぶ。既定は送る）
    sorry_on = request.args.get("sorry", "1") == "1"
    sorry_text = (request.args.get("sorry_message", "") or load_json("sorry_message", default="")
                  or DEFAULT_SORRY_MESSAGE).strip()
    if request.args.get("sorry_message"):
        save_json("sorry_message", sorry_text)
    sorry_items = build_unassigned_notification(schedule, sorry_text)
    if sorry_on:
        items = items + sorry_items

    if request.args.get("send") == "1":
        result = send_notifications(items)
        return (
            f"<pre>{result}</pre>"
            f'<p><a href="{admin_link("panel")}">管理画面に戻る</a></p>'
        )

    total = sum(len(i["user_ids"]) for i in items)
    preview = [f"{total}通を送信します。内容を確認してください。", ""]
    for i in items:
        if i.get("names"):
            preview.append(f"── {i['name']}（{len(i['user_ids'])}人: {'、'.join(i['names'])}）")
        else:
            preview.append(f"── {i['name']}（{len(i['user_ids'])}人）")
        preview.append(i["text"])
        preview.append("")

    send_url = admin_link(
        "notify", send="1", message=message or DEFAULT_NOTIFY_MESSAGE,
        sorry="1" if sorry_on else "0", sorry_message=sorry_text,
    )
    esc = html_lib.escape
    sorry_box = ""
    if sorry_items:
        token = request.args.get("token", "")
        hidden = f'<input type="hidden" name="token" value="{esc(token)}">' if token else ""
        sorry_box = (
            '<form method="get" action="/admin/notify" style="margin:14px 0;padding:12px;border:1px solid #ddd;'
            'border-radius:8px;max-width:640px">'
            f'{hidden}<input type="hidden" name="message" value="{esc(message or DEFAULT_NOTIFY_MESSAGE)}">'
            '<b>枠に入らなかった人へのお知らせ</b>'
            f'<p style="font-size:13px;color:#666;margin:6px 0">回答したのにレッスンが入らなかった人: '
            f'{esc("、".join(sorry_items[0]["names"]))}</p>'
            f'<label><input type="checkbox" name="sorry" value="1"{" checked" if sorry_on else ""}> 一緒に送る</label>'
            '<input type="hidden" name="sorry" value="0">'
            f'<textarea name="sorry_message" rows="4" style="display:block;width:100%;margin-top:8px;'
            f'font-size:14px;box-sizing:border-box">{esc(sorry_text)}</textarea>'
            '<button style="margin-top:8px">この内容でプレビューを更新</button>'
            '</form>'
        )
    return (
        f"<pre>{esc(chr(10).join(preview))}</pre>"
        f"{sorry_box}"
        f'<p><a href="{send_url}" '
        f"onclick=\"return confirm('{total}通を送信します。よろしいですか？')\">"
        f"この内容で送信する</a>　"
        f'<a href="{admin_link("panel")}">やめる</a></p>'
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

    # 回答期間中はサーバーを起こしたままにし、期限を過ぎたら自動で止まるようにする
    import keepalive
    note = keepalive.arm("")
    if note:
        result = f"{result}\n\n{note}"

    return f"<pre>{result}</pre>"


@app.route("/admin/summarize", methods=["GET"])
def admin_summarize():
    """?token=... にアクセスすると現在の投票状況を集計して表示する"""
    check_admin_token()
    from schedule_tools import summarize_replies

    result = summarize_replies()
    return f"<pre>{result}</pre>"


RESET_PAGE_HTML = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>回答のリセット</title><style>
*{box-sizing:border-box}body{margin:0;background:#f5f7f8;color:#202428;font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans",sans-serif;padding:24px}
.box{max-width:560px;margin:0 auto;background:#fff;border:1px solid #dfe3e6;border-radius:12px;padding:26px}
h1{font-size:20px;margin:0 0 6px}.lead{color:#687078;font-size:13px;margin:0 0 20px}
.warn{background:#fff6f5;border:1px solid #f0cfcb;border-radius:8px;padding:14px 16px;margin:0 0 20px}
.warn b{color:#a52b21}
table{width:100%;border-collapse:collapse;margin:0 0 20px}
td{padding:8px 0;border-bottom:1px solid #f0f2f3;font-size:14px}
td.num{text-align:right;font-weight:700}
tr:last-child td{border-bottom:0}
.safe{background:#f2f8f5;border:1px solid #cfe4d9;border-radius:8px;padding:14px 16px;margin:0 0 20px;font-size:13px;color:#0f6e56}
.row{display:flex;gap:10px}
button{height:44px;border-radius:8px;font-size:15px;cursor:pointer}
.danger{flex:1;background:#c0392b;border:1px solid #c0392b;color:#fff;font-weight:700}
.cancel{flex:1;background:#fff;border:1px solid #bec5c9;color:#202428;text-decoration:none;display:flex;align-items:center;justify-content:center}
</style></head><body><main class="box">
<h1>回答をリセットします</h1>
<p class="lead">次回の日程調整を始める前に、前回の回答を消す操作です。</p>
<div class="warn"><b>この操作は元に戻せません。</b><br>ただし消す直前の控えを1つだけ自動で保存するので、間違えた場合はすぐ戻せます。</div>
<table>__ROWS__</table>
<div class="safe">__BACKUP__</div>
<form method="post" action="/admin/reset">
<input type="hidden" name="token" value="__TOKEN__">
<div class="row">
<a class="cancel" href="/admin/panel?token=__TOKEN__">やめる</a>
<button class="danger" type="submit">控えを取って削除する</button>
</div></form>
</main></body></html>"""


@app.route("/admin/reset", methods=["GET"])
def admin_reset_confirm():
    """確認画面を出すだけ。**このURLを開いてもデータは消えない。**
    以前はGETで即削除していたため、URLを踏むだけで事故る恐れがあった。"""
    check_admin_token()
    from schedule_tools import reset_counts, backup_info

    rows = "".join(
        f"<tr><td>{label}</td><td class='num'>{count}件</td></tr>"
        for label, count in reset_counts()
    )
    info = backup_info()
    if info:
        detail = "、".join(f"{l} {c}件" for l, c in info["counts"])
        backup = f"現在の控え: {info['at'][:16].replace('T', ' ')}（{detail}）"
    else:
        backup = "控えはまだありません。今回の削除時に作成されます。"

    return (
        RESET_PAGE_HTML.replace("__ROWS__", rows)
        .replace("__BACKUP__", backup)
        .replace("__TOKEN__", admin_form_token())
    )


@app.route("/admin/reset", methods=["POST"])
def admin_reset():
    """実際に削除する。削除前に必ず控えを取る。"""
    check_admin_token()
    from schedule_tools import backup_replies, reset_replies

    backup_replies()
    result = reset_replies()
    panel = admin_link("panel")
    undo = admin_link("reset/restore")
    return (
        f"<pre>{result}\n\n控えを保存しました。間違えた場合はすぐ戻せます。</pre>"
        f'<p><a href="{undo}">元に戻す</a>　<a href="{panel}">管理画面に戻る</a></p>'
    )


@app.route("/admin/reset/restore", methods=["GET", "POST"])
def admin_reset_restore():
    """控えから元に戻す。GETは確認、POSTで実行。"""
    check_admin_token()
    from schedule_tools import restore_replies, backup_info

    panel = admin_link("panel")

    if request.method == "POST":
        result = restore_replies()
        return f"<pre>{result}</pre><p><a href=\"{panel}\">管理画面に戻る</a></p>"

    info = backup_info()
    if not info:
        return f'<pre>戻せる控えがありません。</pre><p><a href="{panel}">管理画面に戻る</a></p>'

    detail = "、".join(f"{l} {c}件" for l, c in info["counts"])
    return (
        f"<pre>{info['at'][:16].replace('T', ' ')} の控えに戻します。\n\n{detail}\n\n"
        "今のデータは上書きされます。</pre>"
        f'<form method="post" action="/admin/reset/restore">'
        f'<input type="hidden" name="token" value="{admin_form_token()}">'
        f'<button type="submit">この控えに戻す</button></form>'
        f'<p><a href="{panel}">やめる</a></p>'
    )


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
                        text=f"{display_name}さん、友だち追加ありがとうございます！\n\n画面下の「メニュー」から、次のレッスンの確認や曲の記録ができます。日程調整の際は、こちらからご案内します。"
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
    """テキストが届いたら、このアカウントの使い方と連絡先を案内する。
    Reply APIなので何通返しても無料通数は消費しない。"""
    user_id = event.source.user_id

    with ApiClient(configuration) as api_client:
        line_api = MessagingApi(api_client)
        display_name = get_display_name(line_api, user_id)
        upsert_member(user_id, display_name)

        line_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=str(AUTO_REPLY))],
            )
        )


def push_text_message(user_id: str, text: str) -> None:
    """Send one teacher-approved text message on the active tenant channel."""
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(
                to=user_id,
                messages=[TextMessage(text=str(text)[:5000])],
            )
        )


# --- 生徒カルテ ------------------------------------------------------------
# Blueprintは1回だけ登録し、公開可否と設定値はrequestのtenantで切り替える。
from carte import create_carte_blueprint

app.register_blueprint(
    create_carte_blueprint(
        verify_liff_user, upsert_member, ADMIN_TOKEN, CARTE_LIFF_ID,
        lambda user_id, text: push_text_message(user_id, text),
    )
)


@app.before_request
def _block_disabled_features():
    """日程調整を使わない地域では、日程調整の画面を閉じる。
    SCHEDULE_ENABLEDが未設定なら何もしない（＝従来どおり全部開いている）。"""
    if (request.endpoint or "").startswith("teacher_admin."):
        g.tenant = None
        return None

    tenant = (request.view_args or {}).pop("tenant", None)
    if tenant is not None:
        if tenant not in TENANTS:
            abort(404)
        g.tenant = tenant
    else:
        g.tenant = DEFAULT_TENANT

    path = request.path
    if (path.endswith("/carte") or "/api/carte/" in path or "/admin/carte" in path) and not CARTE_ENABLED:
        return "この地域では生徒カルテを使っていません。", 404
    if SCHEDULE_ENABLED:
        return None
    tenant_path = path.removeprefix(f"/{g.tenant}") or "/"
    if any(tenant_path == p or tenant_path.startswith(p + "/") for p in SCHEDULE_PATHS):
        return "この地域では日程調整を使っていません。", 404
    return None


@app.after_request
def _tenant_urls(response):
    """Keep generated admin/LIFF links inside the active tenant namespace."""
    tenant = getattr(g, "tenant", None)
    if not tenant:
        return response
    location = response.headers.get("Location", "")
    if location.startswith("/") and not location.startswith(f"/{tenant}/"):
        response.headers["Location"] = f"/{tenant}{location}"
    if response.mimetype == "text/html":
        text = response.get_data(as_text=True)
        for root in ("admin", "liff", "carte", "api/carte"):
            text = text.replace(f'"/{root}', f'"/{tenant}/{root}')
            text = text.replace(f"'/{root}", f"'/{tenant}/{root}")
        response.set_data(text)
    return response


def _register_tenant_routes():
    """Expose all feature pages under /kansai and /kanto."""
    for rule in list(app.url_map.iter_rules()):
        if rule.endpoint == "static" or rule.rule in {"/", "/healthz", "/webhook"}:
            continue
        methods = sorted(rule.methods - {"HEAD", "OPTIONS"})
        endpoint = "tenant_" + rule.endpoint.replace(".", "_")
        app.add_url_rule(
            f"/<tenant>{rule.rule}",
            endpoint=endpoint,
            view_func=app.view_functions[rule.endpoint],
            methods=methods,
        )
    app.add_url_rule(
        "/webhook/<tenant>",
        endpoint="tenant_webhook",
        view_func=webhook,
        methods=["POST"],
    )


_register_tenant_routes()

# These routes are intentionally registered after tenant route cloning. They
# are one shared teacher portal, not regional copies.
from admin_portal import create_admin_portal_blueprint

app.register_blueprint(create_admin_portal_blueprint())


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
