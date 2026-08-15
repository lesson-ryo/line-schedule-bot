"""Teacher-facing status checks and recoverable application snapshots."""

from __future__ import annotations

from datetime import datetime, timezone

from flask import g

from storage import load_json, save_json, storage_status


SCHEDULE_KEYS = (
    "members",
    "candidates",
    "votes",
    "skips",
    "locations",
    "comments",
    "quotas",
    "groups",
    "assignment",
    "reset_backup",
    "deadline",
    "comment_label",
    "notify_message",
    "reminders",
    "schedule_targets",
)
CARTE_KEYS = (
    "carte:progress",
    "carte:history",
    "carte:prefs",
    "carte:members",
    "carte:requests",
    "carte:custom_materials",
    "carte:notifications",
    "carte:material_history",
)
MAX_SNAPSHOTS = 14

DICT_KEYS = {"quotas", "groups", "reset_backup", "carte:prefs"}
TEXT_KEYS = {"deadline", "comment_label", "notify_message"}


def _default_for(key: str):
    if key in DICT_KEYS:
        return {}
    if key in TEXT_KEYS:
        return ""
    return []


def _with_tenant(name: str, callback):
    previous = getattr(g, "tenant", None)
    g.tenant = name
    try:
        return callback()
    finally:
        g.tenant = previous


def build_snapshot() -> dict:
    schedules = {}
    for tenant in ("kansai", "kanto"):
        schedules[tenant] = _with_tenant(
            tenant,
            lambda: {key: load_json(key, default=_default_for(key)) for key in SCHEDULE_KEYS},
        )

    # carte:* keys always resolve to the shared carte namespace.
    carte = _with_tenant(
        "kanto",
        lambda: {key: load_json(key, default=_default_for(key)) for key in CARTE_KEYS},
    )
    return {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "schedules": schedules,
        "carte": carte,
    }


def save_snapshot() -> dict:
    snapshot = build_snapshot()
    snapshots = _with_tenant(
        "kanto", lambda: load_json("carte:backups", default=[])
    )
    if not isinstance(snapshots, list):
        snapshots = []
    snapshots.append(snapshot)
    _with_tenant(
        "kanto",
        lambda: save_json("carte:backups", snapshots[-MAX_SNAPSHOTS:]),
    )
    return snapshot


def backup_info() -> dict:
    snapshots = _with_tenant(
        "kanto", lambda: load_json("carte:backups", default=[])
    )
    if not isinstance(snapshots, list):
        snapshots = []
    latest = snapshots[-1] if snapshots else {}
    return {
        "count": len(snapshots),
        "latest_at": latest.get("created_at", "") if isinstance(latest, dict) else "",
    }


def list_snapshots() -> list[dict]:
    snapshots = _with_tenant(
        "kanto", lambda: load_json("carte:backups", default=[])
    )
    if not isinstance(snapshots, list):
        return []
    out = []
    for index, snapshot in enumerate(snapshots):
        if not isinstance(snapshot, dict):
            continue
        schedules = snapshot.get("schedules") or {}
        carte = snapshot.get("carte") or {}
        out.append(
            {
                "index": index,
                "created_at": snapshot.get("created_at", ""),
                "kansai_members": len((schedules.get("kansai") or {}).get("members") or []),
                "kanto_members": len((schedules.get("kanto") or {}).get("members") or []),
                "carte_progress": len(carte.get("carte:progress") or []),
            }
        )
    return out


def restore_snapshot(index: int) -> dict:
    """Restore one stored generation after saving the current state as a safety copy."""
    snapshots = _with_tenant(
        "kanto", lambda: load_json("carte:backups", default=[])
    )
    if not isinstance(snapshots, list) or index < 0 or index >= len(snapshots):
        raise ValueError("復元するバックアップが見つかりません。")
    snapshot = snapshots[index]
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("version") != 1
        or not isinstance(snapshot.get("schedules"), dict)
        or not isinstance(snapshot.get("carte"), dict)
    ):
        raise ValueError("バックアップの形式が正しくありません。")

    safety = save_snapshot()
    schedules = snapshot["schedules"]
    for tenant in ("kansai", "kanto"):
        data = schedules.get(tenant)
        if not isinstance(data, dict):
            raise ValueError(f"{tenant}の日程データが見つかりません。")

        def restore_schedule():
            for key in SCHEDULE_KEYS:
                save_json(key, data.get(key, _default_for(key)))

        _with_tenant(tenant, restore_schedule)

    carte = snapshot["carte"]

    def restore_carte():
        for key in CARTE_KEYS:
            save_json(key, carte.get(key, _default_for(key)))

    _with_tenant("kanto", restore_carte)
    return {
        "restored_at": snapshot.get("created_at", ""),
        "safety_at": safety.get("created_at", ""),
    }


def system_status() -> dict:
    from carte import load_materials
    from tenant_config import TENANTS

    status = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "storage": storage_status(),
        "sheet": {"ok": False, "count": 0, "error": ""},
        "tenants": {},
        "backup": backup_info(),
    }
    try:
        materials = load_materials(force=True)
        status["sheet"] = {"ok": True, "count": len(materials), "error": ""}
    except Exception as exc:
        status["sheet"]["error"] = str(exc)[:300]

    for name, tenant in TENANTS.items():
        status["tenants"][name] = {
            "schedule_enabled": tenant.schedule_enabled,
            "carte_enabled": tenant.carte_enabled,
            "line_configured": bool(
                tenant.channel_access_token and tenant.channel_secret
            ),
        }

    try:
        import keepalive

        status["keepalive"] = keepalive.status()
    except Exception as exc:
        status["keepalive"] = {"configured": False, "error": str(exc)[:300]}
    return status
