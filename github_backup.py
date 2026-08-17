"""Daily off-site backup of Upstash data to a private GitHub repo.

Upstashの無料プランはDaily Backup機能が使えない（要クレジットカード）ため、
アプリ側からUpstashの中身をJSONで書き出し、Upstashとは別のGitHubリポジトリに
1日1回コミットすることで、Upstash自体に何かあっても復旧できるようにする。

トリガーは /healthz。cron-job.orgからの10分おきpingのついでに「今日はまだ
バックアップしていなければ実行する」という形で動く（自動LINE通知と同じ仕組み）。

必要な環境変数:
  GITHUB_BACKUP_TOKEN … バックアップ専用リポジトリへの書き込み権限だけを持つ
                         GitHubのFine-grained personal access token
  GITHUB_BACKUP_REPO  … "owner/repo" 形式（例: lesson-ryo/line-schedule-bot-backups）

どちらも未設定なら、この機能は何もしない（healthzは通常通り成功する）。
"""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime

import requests

from storage import load_json, save_json
from lesson_operations import tenant_scope

TOKEN = os.environ.get("GITHUB_BACKUP_TOKEN", "")
REPO = os.environ.get("GITHUB_BACKUP_REPO", "").strip().strip("/")
BRANCH = os.environ.get("GITHUB_BACKUP_BRANCH", "main")

API = "https://api.github.com"
MAX_RUN_LOG = 30


def _now_jst() -> datetime:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Tokyo"))


def is_configured() -> bool:
    return bool(TOKEN and REPO and "/" in REPO)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _runs() -> list[dict]:
    with tenant_scope("kanto"):
        runs = load_json("backup_runs", default=[])
    return runs if isinstance(runs, list) else []


def _save_runs(runs: list[dict]) -> None:
    with tenant_scope("kanto"):
        save_json("backup_runs", runs[-MAX_RUN_LOG:])


def _last_run() -> dict:
    runs = _runs()
    return runs[-1] if runs else {}


def _put_file(path: str, content_bytes: bytes, message: str) -> None:
    url = f"{API}/repos/{REPO}/contents/{path}"
    # 同じパスに既存ファイルがあれば sha が必要（上書き用）。無ければ新規作成。
    sha = None
    existing = requests.get(url, headers=_headers(), params={"ref": BRANCH}, timeout=15)
    if existing.status_code == 200:
        sha = existing.json().get("sha")

    payload = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode("ascii"),
        "branch": BRANCH,
    }
    if sha:
        payload["sha"] = sha

    res = requests.put(url, headers=_headers(), json=payload, timeout=20)
    if res.status_code not in (200, 201):
        raise RuntimeError(f"{res.status_code} {res.text[:300]}")


def run_backup(reason: str = "manual") -> dict:
    """今すぐバックアップを1回実行する。管理画面の手動実行からも使う。"""
    if not is_configured():
        return {"skipped": "not_configured"}

    from maintenance import build_snapshot

    snapshot = build_snapshot()
    now = _now_jst()
    date_str = now.strftime("%Y-%m-%d")
    body = json.dumps(snapshot, ensure_ascii=False, indent=2).encode("utf-8")

    entry = {"date": date_str, "started_at": now.isoformat(), "reason": reason}
    try:
        _put_file(
            f"backups/{date_str}.json",
            body,
            f"Backup {date_str} ({reason})",
        )
        entry.update(completed_at=_now_jst().isoformat(), ok=True, error="")
    except Exception as exc:
        entry.update(completed_at=_now_jst().isoformat(), ok=False, error=str(exc)[:300])

    runs = _runs()
    runs.append(entry)
    _save_runs(runs)
    return entry


def run_due_backup() -> dict:
    """healthzから呼ばれる。今日分がまだなら1回だけ実行する。"""
    if not is_configured():
        return {"skipped": "not_configured"}

    now = _now_jst()
    last = _last_run()
    if last.get("date") == now.strftime("%Y-%m-%d") and last.get("ok"):
        return {"skipped": "already_done_today"}

    # 同時に複数リクエストが来ても二重実行しないよう、進行中フラグを見る。
    if last.get("date") == now.strftime("%Y-%m-%d") and not last.get("completed_at"):
        return {"skipped": "in_progress"}

    return run_backup(reason="auto")


def status() -> dict:
    if not is_configured():
        return {"configured": False}
    last = _last_run()
    return {
        "configured": True,
        "repo": REPO,
        "last_run": last,
    }
