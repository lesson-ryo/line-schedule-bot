"""
日程候補の一斉送信 & 集計ツール（タップ投票版・外部API不要）

メンバーにはボタン付きメッセージ（Flex Message）を送り、都合の良い候補をタップしてもらいます。
タップは何度でも可能（トグル式）で、集計はvotes.jsonの構造化データを読むだけなので
自然文解析やAPIは一切不要です。

使い方（デプロイしたサーバーと同じ環境変数を設定した上で、手元やRenderのShellから実行）:

  # 1) 登録済み全メンバーに日程候補ボタン付きメッセージを送る（候補は1つずつ引数で渡す）
  python schedule_tools.py send "8/5(水) 14:00-" "8/6(木) 10:00-" "8/7(金) 15:00-"

  # 2) メンバーがボタンをタップしたら、溜まった投票を集計する
  python schedule_tools.py summarize

  # 3) 集計が終わったら投票データをリセットしたいとき
  python schedule_tools.py reset
"""

import os
import sys
import json
from pathlib import Path

from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    PushMessageRequest,
    FlexMessage,
    FlexContainer,
)

CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)

BASE_DIR = Path(__file__).parent
MEMBERS_FILE = BASE_DIR / "members.json"
VOTES_FILE = BASE_DIR / "votes.json"
CANDIDATES_FILE = BASE_DIR / "candidates.json"


def load_json(path, default=None):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default if default is not None else []


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def build_flex_contents(candidates: list[str]) -> dict:
    """候補ごとにタップ用ボタンを並べたFlex Messageのcontentsを組み立てる"""
    buttons = []
    for i, c in enumerate(candidates, start=1):
        buttons.append(
            {
                "type": "button",
                "style": "primary",
                "color": "#06C755",
                "margin": "md",
                "action": {
                    "type": "postback",
                    "label": f"{i}. {c}"[:40],  # LINEのボタンラベルは40文字まで
                    "data": f"action=vote&candidate={i}",
                    "displayText": f"「{i}. {c}」をタップしました",
                },
            }
        )

    return {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": "都合の良い日程をすべてタップしてください（複数選択・解除も可）",
                    "wrap": True,
                    "weight": "bold",
                    "size": "sm",
                },
                *buttons,
            ],
        },
    }


def send_schedule(candidates: list[str]):
    """membersに登録されている全員に日程候補ボタン付きメッセージをPush送信する（Push APIなので無料通数を消費）"""
    members = load_json(MEMBERS_FILE)
    if not members:
        print("membersが空です。先にLINE公式アカウントを友だち追加してもらってください。")
        return

    save_json(CANDIDATES_FILE, candidates)

    contents_dict = build_flex_contents(candidates)
    flex_message = FlexMessage(
        alt_text="日程候補が届きました。タップして回答してください。",
        contents=FlexContainer.from_dict(contents_dict),
    )

    with ApiClient(configuration) as api_client:
        line_api = MessagingApi(api_client)
        for m in members:
            line_api.push_message(
                PushMessageRequest(to=m["user_id"], messages=[flex_message])
            )
    print(f"{len(members)}人に日程候補（タップ式）を送信しました。")


def summarize_replies():
    """votes.jsonの構造化データを集計して候補ごとの得票数を表示する"""
    candidates = load_json(CANDIDATES_FILE, default=[])
    votes = load_json(VOTES_FILE)

    if not candidates:
        print("candidates.jsonが見つかりません。先に send コマンドで候補を送信してください。")
        return

    if not votes:
        print("まだ投票がありません。")
        return

    tally = {i: [] for i in range(1, len(candidates) + 1)}
    for v in votes:
        idx = v["candidate_index"]
        if idx in tally:
            tally[idx].append(v["display_name"])

    print("=== 集計結果 ===\n")
    for i, c in enumerate(candidates, start=1):
        names = tally[i]
        print(f"{i}. {c} — {len(names)}人: {', '.join(names) if names else 'なし'}")

    best = max(tally, key=lambda k: len(tally[k]))
    print(f"\n最多得票: 候補{best}「{candidates[best-1]}」（{len(tally[best])}人）")


def reset_replies():
    save_json(VOTES_FILE, [])
    print("投票データをリセットしました。")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1]

    if command == "send":
        if len(sys.argv) < 3:
            print("送信する候補日程を1つ以上指定してください。")
            sys.exit(1)
        send_schedule(sys.argv[2:])
    elif command == "summarize":
        summarize_replies()
    elif command == "reset":
        reset_replies()
    else:
        print(f"不明なコマンドです: {command}")
        print(__doc__)
