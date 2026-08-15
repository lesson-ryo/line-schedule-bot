"""
共有ストレージ層

Renderの無料プランはディスクを保持しないため、15分操作がないとスリープし、
再起動・再デプロイのたびにローカルファイルの中身が消えてしまう
（friends一覧や投票データがリセットされる原因）。

これを避けるため、環境変数 UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN が
設定されていれば、Upstash(無料のRedis)にデータを保存する。
未設定の場合はローカルの.jsonファイルにフォールバックする（ローカル開発用）。

app.py / schedule_tools.py はどちらもここの load_json / save_json を使う。

複数のBot（例: 関西用と関東用）で同じUpstashを共有する場合は、環境変数
STORAGE_PREFIX に "kanto:" のような接頭辞を設定すると、キーが分離されて
データが混ざらない。未設定なら接頭辞なし（既存データをそのまま使える）。
"""

import os
import json
from pathlib import Path

import requests

UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")

BASE_DIR = Path(__file__).parent


def _using_upstash() -> bool:
    return bool(UPSTASH_URL and UPSTASH_TOKEN)


def _key(key: str) -> str:
    """現在のtenantの接頭辞を付け、地域間のデータ混入を防ぐ。"""
    from tenant_config import get_tenant

    prefix = get_tenant().storage_prefix
    return f"{prefix}{key}" if prefix else key


def _redis_command(*args):
    res = requests.post(
        UPSTASH_URL,
        headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
        json=list(args),
        timeout=10,
    )
    res.raise_for_status()
    return res.json().get("result")


def load_json(key: str, default=None):
    """key: 'members' / 'votes' / 'candidates' のような文字列。"""
    name = _key(key)

    if _using_upstash():
        raw = _redis_command("GET", name)
        if raw is None:
            return default if default is not None else []
        return json.loads(raw)

    path = BASE_DIR / f"{name.replace(':', '_')}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default if default is not None else []


def save_json(key: str, data):
    name = _key(key)

    if _using_upstash():
        _redis_command("SET", name, json.dumps(data, ensure_ascii=False))
        return

    path = BASE_DIR / f"{name.replace(':', '_')}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
