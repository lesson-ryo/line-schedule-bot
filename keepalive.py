"""
サーバーのスリープ防止（キープアライブ）の自動制御

Renderの無料プランは15分アクセスがないとスリープし、次のアクセスで50〜60秒待たされる。
これを防ぐため cron-job.org から /healthz を10分おきに叩いてもらっているが、
ずっと起こしたままだと無料枠（月750インスタンス時間）を無駄に消費してしまう。

そこで、管理画面から日程候補を送信したタイミングで cron-job.org のAPIを叩き、

  1. pingジョブを有効にする（enabled = true）
  2. 回答期限の日の23:59:59に自動で失効するようセットする（schedule.expiresAt）

ようにする。期限が過ぎると cron-job.org 側が勝手にジョブを止めるので、
こちらから停止の操作をする必要がない。

必要な環境変数:
  CRONJOB_API_KEY … cron-job.orgのConsole → Settings → API Keys で発行したキー
  CRONJOB_JOB_ID  … pingジョブのID（Consoleでジョブを開いたときのURL末尾の数字）

どちらも未設定なら、この機能は何もしない（送信自体は普通に成功する）。
"""

import os

import requests

API_KEY = os.environ.get("CRONJOB_API_KEY", "")
JOB_ID = os.environ.get("CRONJOB_JOB_ID", "")

ENDPOINT = "https://api.cron-job.org"
TIMEZONE = "Asia/Tokyo"


def is_configured() -> bool:
    return bool(API_KEY and JOB_ID)


def _patch(job: dict) -> None:
    res = requests.patch(
        f"{ENDPOINT}/jobs/{JOB_ID}",
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
        json={"job": job},
        timeout=10,
    )
    if res.status_code != 200:
        raise RuntimeError(f"{res.status_code} {res.text}")


def arm(deadline: str) -> str:
    """pingを有効にし、回答期限の日の終わりに自動で止まるようセットする。

    deadline は "YYYY-MM-DD"。空文字なら期限なしとして有効化だけ行う。
    戻り値は管理画面に出す1行のメッセージ（失敗しても例外は投げない）。
    """
    if not is_configured():
        return ""

    # cron-job.orgのexpiresAtは YYYYMMDDhhmmss 形式（ジョブのタイムゾーン基準）。0で無期限。
    if deadline:
        expires_at = int(deadline.replace("-", "") + "235959")
        note = f"{deadline} の23:59に自動停止します"
    else:
        expires_at = 0
        note = "期限が未設定のため、自動停止はしません"

    try:
        _patch({
            "enabled": True,
            "schedule": {"timezone": TIMEZONE, "expiresAt": expires_at},
        })
    except Exception as e:
        return f"※ サーバーの起動維持の設定に失敗しました（{e}）。cron-job.orgの画面を確認してください。"

    return f"サーバーの起動維持をONにしました。{note}"


def disarm() -> str:
    """pingを今すぐ止める（期限前に打ち切りたいとき用）。"""
    if not is_configured():
        return ""
    try:
        _patch({"enabled": False})
    except Exception as e:
        return f"※ 起動維持の停止に失敗しました（{e}）。"
    return "サーバーの起動維持をOFFにしました。"


def status() -> dict:
    """現在のジョブの状態を返す（管理画面の表示用）。"""
    if not is_configured():
        return {"configured": False}
    try:
        res = requests.get(
            f"{ENDPOINT}/jobs/{JOB_ID}",
            headers={"Authorization": f"Bearer {API_KEY}"},
            timeout=10,
        )
        res.raise_for_status()
        d = res.json().get("jobDetails", {})
        return {
            "configured": True,
            "enabled": bool(d.get("enabled")),
            "expires_at": d.get("schedule", {}).get("expiresAt", 0),
        }
    except Exception as e:
        return {"configured": True, "error": str(e)}
