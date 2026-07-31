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
