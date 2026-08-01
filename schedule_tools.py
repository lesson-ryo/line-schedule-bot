"""
日程候補の一斉送信 & 集計ツール（タップ投票版・外部API不要）

メンバーにはボタン付きメッセージ（Flex Message）を送り、都合の良い候補をタップしてもらいます。
タップは何度でも可能（トグル式）で、集計はvotes.jsonの構造化データを読むだけなので
自然文解析やAPIは一切不要です。

使い方（デプロイしたサーバーと同じ環境変数を設定した上で、手元やRenderのShellから実行）:

  # 0) 送信先を絞りたい場合、まず登録メンバーの番号を確認する
  python schedule_tools.py members

  # 1) 登録済み全メンバーに日程候補ボタン付きメッセージを送る（候補は1つずつ引数で渡す）
  python schedule_tools.py send "8/5(水) 14:00-" "8/6(木) 10:00-" "8/7(金) 15:00-"

  # 1') 特定のメンバーだけに送りたい場合（membersで確認した番号を --to で指定、カンマ区切り）
  python schedule_tools.py send "8/5(水) 14:00-" "8/6(木) 10:00-" --to 1,3

  # 2) メンバーがボタンをタップしたら、溜まった投票を集計する
  python schedule_tools.py summarize

  # 3) 集計が終わったら投票データをリセットしたいとき
  python schedule_tools.py reset
"""

import os
import sys

from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    PushMessageRequest,
    FlexMessage,
    FlexContainer,
    TextMessage,
)

from storage import load_json, save_json

CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LIFF_ID = os.environ.get("LIFF_ID", "")

# 候補数がこれを超えたら、ボタン1つずつのFlex Messageではなく
# LIFF(チェックボックスフォーム)へのリンクを送る。0にすると常にLIFFフォームを使う。
LIFF_THRESHOLD = 0

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)


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


DEFAULT_MESSAGE = "日程調整のお願いです。ボタンをタップして、都合の良い日程をすべて選んでください。"


def build_liff_link_contents(num_candidates: int, message: str = "", deadline: str = "") -> dict:
    """ボタン1つだけ(LIFFフォームを開く)のシンプルなFlex Message。
    messageを指定するとメッセージ本文を、deadlineを指定すると末尾に回答期限を入れられる。"""
    body = (message or DEFAULT_MESSAGE).strip()
    if deadline:
        body = f"{body}\n\n回答期限: {deadline}"
    return {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": body,
                    "wrap": True,
                    "weight": "bold",
                    "size": "sm",
                },
                {
                    "type": "text",
                    "text": f"候補: {num_candidates}件",
                    "size": "xs",
                    "color": "#999999",
                    "margin": "sm",
                },
                {
                    "type": "button",
                    "style": "primary",
                    "color": "#06C755",
                    "margin": "md",
                    "action": {
                        "type": "uri",
                        "label": "日程を選ぶ",
                        "uri": f"https://liff.line.me/{LIFF_ID}",
                    },
                },
            ],
        },
    }


def list_members() -> str:
    """登録メンバーに番号を振って一覧表示する（送信先を絞り込むときに使う番号）"""
    members = load_json("members")
    if not members:
        return "メンバーがまだ登録されていません。先にLINE公式アカウントを友だち追加してもらってください。"

    lines = ["=== 登録メンバー一覧 ===", ""]
    for i, m in enumerate(members, start=1):
        lines.append(f"{i}. {m['display_name']}")
    return "\n".join(lines)


def send_schedule(
    candidates: list[str],
    member_indices: list[int] | None = None,
    message: str = "",
    deadline: str = "",
) -> str:
    """日程候補ボタン付きメッセージをPush送信する（Push APIなので無料通数を消費）。
    member_indices を指定すると list_members() の番号で送信先を絞り込める。省略時は全員に送信。
    message を指定するとメッセージ本文を差し替えられる。"""
    members = load_json("members")
    if not members:
        return "membersが空です。先にLINE公式アカウントを友だち追加してもらってください。"

    if member_indices:
        target_members = [members[i - 1] for i in member_indices if 0 < i <= len(members)]
        if not target_members:
            return f"指定されたメンバー番号が見つかりません（登録は{len(members)}人です。list_membersで確認してください）。"
    else:
        target_members = members

    save_json("candidates", candidates)

    if len(candidates) > LIFF_THRESHOLD:
        if not LIFF_ID:
            return (
                "LIFFフォームでの送信が必要ですが、環境変数 LIFF_ID が設定されていません。"
                "LINE DevelopersでLIFFアプリを登録し、RenderにLIFF_IDを設定してください。"
            )
        contents_dict = build_liff_link_contents(len(candidates), message, deadline)
        alt_text = (message or DEFAULT_MESSAGE).strip()[:100]
        mode_label = "LIFFフォーム式"
    else:
        contents_dict = build_flex_contents(candidates)
        alt_text = "日程候補が届きました。タップして回答してください。"
        mode_label = "タップ式"

    flex_message = FlexMessage(
        alt_text=alt_text,
        contents=FlexContainer.from_dict(contents_dict),
    )

    with ApiClient(configuration) as api_client:
        line_api = MessagingApi(api_client)
        for m in target_members:
            line_api.push_message(
                PushMessageRequest(to=m["user_id"], messages=[flex_message])
            )
    names = [m["display_name"] for m in target_members]
    return f"{len(target_members)}人に日程候補（{mode_label}）を送信しました。\n送信先: {names}\n候補: {candidates}"


DEFAULT_NOTIFY_MESSAGE = "レッスン日程が確定しましたのでお知らせします。"
DEFAULT_REMIND_MESSAGE = "日程調整の回答がまだのようです。お手数ですがご回答をお願いします。"
DEFAULT_FOLLOWUP_MESSAGE = (
    "申し訳ありません。ご希望いただいた時間は他の方で埋まってしまいました。\n"
    "下記の空いている時間から、ご都合の良いものをこのトークで返信いただけますか。"
)


def send_text_to(user_ids: list[str], text: str) -> str:
    """指定した人にテキストメッセージを送る（Push）"""
    if not user_ids:
        return "送信対象がいません。"

    sent, failed = 0, []
    with ApiClient(configuration) as api_client:
        line_api = MessagingApi(api_client)
        for user_id in user_ids:
            try:
                line_api.push_message(
                    PushMessageRequest(to=user_id, messages=[TextMessage(text=text)])
                )
                sent += 1
            except Exception as e:
                failed.append(f"{user_id}: {e}")

    lines = [f"{sent}通を送信しました。"]
    if failed:
        lines.append("")
        lines.append("送信できなかった分:")
        lines.extend(f"  {f}" for f in failed)
    return "\n".join(lines)


def build_remind_text(message: str = "", deadline: str = "") -> str:
    """未回答者へのリマインド本文"""
    lines = [(message or DEFAULT_REMIND_MESSAGE).strip()]
    if deadline:
        lines.append("")
        lines.append(f"回答期限: {deadline}")
    if LIFF_ID:
        lines.append("")
        lines.append(f"回答はこちら → https://liff.line.me/{LIFF_ID}")
    return "\n".join(lines)


def build_followup_text(free_slots: list[str], message: str = "") -> str:
    """枠が取れなかった人への案内本文（残っている空き枠を添える）"""
    lines = [(message or DEFAULT_FOLLOWUP_MESSAGE).strip()]
    if free_slots:
        lines.append("")
        lines.append("空いている時間:")
        lines.extend(f"  {s}" for s in free_slots)
    else:
        lines.append("")
        lines.append("空いている時間がないため、個別に調整させてください。")
    return "\n".join(lines)


def build_notifications(schedule: list[dict], message: str = "") -> list[dict]:
    """割り当て結果から、1人ずつに送る本文を組み立てる。
    戻り値: [{"user_ids": [...], "name": 表示名, "text": 本文}, ...]"""
    intro = (message or DEFAULT_NOTIFY_MESSAGE).strip()
    items = []

    for s in schedule:
        lines = [intro, ""]
        if s.get("is_group"):
            lines.append(f"グループ: {s['name']}")
        lines.append(f"日時: {s['day']} {s['time']}-{s['end']}")
        if s.get("location"):
            lines.append(f"教室: {s['location']}")

        items.append(
            {
                "user_ids": list(s.get("member_ids", [])),
                "name": s["name"],
                "text": "\n".join(lines),
            }
        )
    return items


def send_notifications(items: list[dict]) -> str:
    """組み立てた本文を各自にPush送信する（1人1通）"""
    if not items:
        return "送信する内容がありません。先に割り当てを実行してください。"

    sent, failed = 0, []
    with ApiClient(configuration) as api_client:
        line_api = MessagingApi(api_client)
        for item in items:
            for user_id in item["user_ids"]:
                try:
                    line_api.push_message(
                        PushMessageRequest(to=user_id, messages=[TextMessage(text=item["text"])])
                    )
                    sent += 1
                except Exception as e:
                    failed.append(f"{item['name']}: {e}")

    lines = [f"{sent}通を送信しました。"]
    if failed:
        lines.append("")
        lines.append("送信できなかった分:")
        lines.extend(f"  {f}" for f in failed)
    return "\n".join(lines)


def _location_lines() -> list[str]:
    """回答者が選んだ教室を整形して返す"""
    locations = load_json("locations")
    if not locations:
        return []
    grouped: dict[str, list[str]] = {}
    for l in locations:
        grouped.setdefault(l["location"], []).append(l["display_name"])
    lines = ["", "=== 教室 ===", ""]
    for loc, names in grouped.items():
        lines.append(f"■ {loc}（{len(names)}人）")
        lines.append(f"  {', '.join(names)}")
    return lines


def _comment_lines() -> list[str]:
    """回答者が書いた連絡事項（任意入力）を整形して返す"""
    comments = load_json("comments")
    if not comments:
        return []
    lines = ["", "=== 連絡事項 ===", ""]
    for c in comments:
        lines.append(f"■ {c['display_name']}")
        lines.append(f"  {c['text']}")
    return lines


def summarize_replies() -> str:
    """投票の構造化データを集計して候補ごとの得票数を文字列で返す"""
    candidates = load_json("candidates", default=[])
    votes = load_json("votes")

    if not candidates:
        return "送信済みの候補が見つかりません。先に候補を送信してください。"

    if not votes:
        return "\n".join(["まだ投票がありません。"] + _location_lines() + _comment_lines())

    tally = {i: [] for i in range(1, len(candidates) + 1)}
    for v in votes:
        idx = v["candidate_index"]
        if idx in tally:
            tally[idx].append(v["display_name"])

    lines = ["=== 集計結果 ===", ""]
    for i, c in enumerate(candidates, start=1):
        names = tally[i]
        lines.append(f"{i}. {c} — {len(names)}人: {', '.join(names) if names else 'なし'}")

    best = max(tally, key=lambda k: len(tally[k]))
    lines.append("")
    lines.append(f"最多得票: 候補{best}「{candidates[best-1]}」（{len(tally[best])}人）")
    lines.extend(_location_lines())
    lines.extend(_comment_lines())

    return "\n".join(lines)


def reset_replies() -> str:
    save_json("votes", [])
    save_json("comments", [])
    save_json("locations", [])
    return "回答データ（日程・教室・連絡事項）をリセットしました。"


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1]

    if command == "members":
        print(list_members())
    elif command == "send":
        rest = sys.argv[2:]
        member_indices = None
        if "--to" in rest:
            idx = rest.index("--to")
            to_value = rest[idx + 1]
            member_indices = [int(x) for x in to_value.split(",") if x.strip()]
            rest = rest[:idx] + rest[idx + 2:]
        if not rest:
            print("送信する候補日程を1つ以上指定してください。")
            sys.exit(1)
        print(send_schedule(rest, member_indices))
    elif command == "summarize":
        print(summarize_replies())
    elif command == "reset":
        print(reset_replies())
    else:
        print(f"不明なコマンドです: {command}")
        print(__doc__)
