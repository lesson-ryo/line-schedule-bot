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
from tenant_config import get_tenant
from werkzeug.local import LocalProxy

CHANNEL_ACCESS_TOKEN = LocalProxy(lambda: get_tenant().channel_access_token)
LIFF_ID = LocalProxy(lambda: get_tenant().liff_id)

# 候補数がこれを超えたら、ボタン1つずつのFlex Messageではなく
# LIFF(チェックボックスフォーム)へのリンクを送る。0にすると常にLIFFフォームを使う。
LIFF_THRESHOLD = 0

configuration = LocalProxy(
    lambda: Configuration(access_token=get_tenant().channel_access_token)
)


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


def build_notifications(schedule: list[dict], message: str = "") -> list[dict]:
    """割り当て結果から、送る相手ごとの本文を組み立てる。

    1件 = 1つの枠（個人またはグループ）。グループは同じ本文をメンバー全員に送る。
    **各自には自分の枠だけを見せる**（他の人の予定は入れない）。

    戻り値: [{"name": 表示名, "user_ids": [...], "text": 本文}, ...]
    """
    members = {m["user_id"]: m["display_name"] for m in load_json("members")}
    intro = (message or DEFAULT_NOTIFY_MESSAGE).strip()

    items = []
    for slot in schedule:
        user_ids = [u for u in slot.get("member_ids", []) if u]
        if not user_ids:
            continue  # 空き枠は送らない

        when = f"{slot.get('day', '')} {slot.get('time', '')}〜{slot.get('end', '')}".strip()
        lines = [intro, "", f"■ {when}"]

        location = slot.get("location", "")
        if location:
            lines.append(f"　教室: {location}")

        if slot.get("is_group"):
            lines.append(f"　レッスン: {slot.get('name', '')}（グループ）")
            absent = [members.get(u, u) for u in slot.get("absent", []) if u]
            if absent:
                lines.append(f"　※ この時間に参加できない方: {', '.join(absent)}")

        lines.append("")
        lines.append("よろしくお願いします。")

        items.append(
            {
                "name": slot.get("name", ""),
                "user_ids": user_ids,
                "text": "\n".join(lines),
            }
        )
    return items


def send_notifications(items: list[dict]) -> str:
    """build_notifications() の結果をPush送信する。
    途中で失敗したらそこで止め、どこまで送ったかを返す（重複送信を避けるため）。"""
    if not items:
        return "送信する内容がありません。"

    sent = 0
    with ApiClient(configuration) as api_client:
        line_api = MessagingApi(api_client)
        for item in items:
            for user_id in item["user_ids"]:
                try:
                    line_api.push_message(
                        PushMessageRequest(
                            to=user_id, messages=[TextMessage(text=item["text"])]
                        )
                    )
                    sent += 1
                except Exception as e:
                    return (
                        f"{sent}通まで送信しましたが、「{item['name']}」への送信でエラーになりました。\n"
                        f"{e}\n\n"
                        "同じ内容を再送すると重複して届く可能性があります。"
                        "LINEの送信履歴を確認してから操作してください。"
                    )
    return f"{sent}通を送信しました。"


def _skip_lines() -> list[str]:
    """「今回は参加できません」と回答した人を整形して返す"""
    skips = load_json("skips")
    if not skips:
        return []
    members = {m["user_id"]: m["display_name"] for m in load_json("members")}
    names = [members.get(u, u) for u in skips]
    return ["", "=== 今回は参加できない人 ===", "", f"  {', '.join(names)}（{len(names)}人）"]


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
        return "\n".join(["まだ投票がありません。"] + _skip_lines() + _location_lines() + _comment_lines())

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
    lines.extend(_skip_lines())
    lines.extend(_location_lines())
    lines.extend(_comment_lines())

    return "\n".join(lines)


# --- リセットと、その取り消し ------------------------------------------------
#
# 誤ってリセットすると回答を集め直すことになるため、消す前に必ず控えを取る。
# 控えは同じUpstashの "reset_backup" に1世代だけ持つ（直前の状態に戻せれば十分）。

RESET_KEYS = ("votes", "comments", "locations", "skips", "assignment")

RESET_LABELS = {
    "votes": "回答",
    "comments": "連絡事項",
    "locations": "教室の選択",
    "skips": "参加できない人",
    "assignment": "割り当て済みの枠",
}


def reset_counts() -> list[tuple[str, int]]:
    """リセットで消えるものを (表示名, 件数) で返す。確認画面に出すため。"""
    out = []
    for key in RESET_KEYS:
        value = load_json(key, default=[])
        out.append((RESET_LABELS.get(key, key), len(value) if hasattr(value, "__len__") else 0))
    return out


def backup_replies() -> dict:
    """消す直前の状態を控える。1世代だけ保持し、古い控えは上書きする。"""
    from datetime import datetime, timezone

    snapshot = {
        "at": datetime.now(timezone.utc).isoformat(),
        "data": {key: load_json(key, default=[]) for key in RESET_KEYS},
    }
    save_json("reset_backup", snapshot)
    return snapshot


def backup_info() -> dict:
    """控えの有無と中身の件数。管理画面の表示用。"""
    snapshot = load_json("reset_backup", default={})
    if not isinstance(snapshot, dict) or not snapshot.get("data"):
        return {}
    data = snapshot["data"]
    return {
        "at": snapshot.get("at", ""),
        "counts": [
            (RESET_LABELS.get(k, k), len(v) if hasattr(v, "__len__") else 0)
            for k, v in data.items()
        ],
    }


def restore_replies() -> str:
    """控えから元に戻す。リセットを取り消したいときに使う。"""
    snapshot = load_json("reset_backup", default={})
    if not isinstance(snapshot, dict) or not snapshot.get("data"):
        return "戻せる控えがありません。"

    restored = []
    for key, value in snapshot["data"].items():
        save_json(key, value)
        restored.append(f"{RESET_LABELS.get(key, key)} {len(value) if hasattr(value, '__len__') else 0}件")
    return "控えから元に戻しました。\n" + " / ".join(restored)


def reset_replies() -> str:
    """回答データを消す。**先に backup_replies() を呼ぶこと。**"""
    for key in RESET_KEYS:
        save_json(key, [])
    return "回答データ（日程・教室・連絡事項・割り当て）をリセットしました。"


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
