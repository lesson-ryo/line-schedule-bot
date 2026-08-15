"""Request-scoped configuration for the regional LINE accounts."""

from __future__ import annotations

import os
from dataclasses import dataclass

from flask import g, has_request_context


TENANT_NAMES = ("kansai", "kanto")
DEFAULT_TENANT = os.environ.get("DEFAULT_TENANT") or (
    "kanto" if os.environ.get("STORAGE_PREFIX", "").startswith("kanto:") else "kansai"
)


def _flag(value: str, default: bool) -> bool:
    value = (value or "").strip().lower()
    return default if not value else value in {"1", "true", "yes", "on"}


def _env(tenant: str, name: str, legacy: str = "") -> str:
    value = os.environ.get(f"{tenant.upper()}_{name}", "")
    if value:
        return value
    return os.environ.get(legacy, "") if legacy else ""


@dataclass(frozen=True)
class TenantConfig:
    name: str
    channel_access_token: str
    channel_secret: str
    admin_token: str
    liff_id: str
    line_channel_id: str
    panel_name: str
    locations: tuple[str, ...]
    auto_reply: str
    carte_liff_id: str
    carte_enabled: bool
    schedule_enabled: bool
    storage_prefix: str


DEFAULT_AUTO_REPLY = "\n".join([
    "このアカウントは日程調整の専用です。",
    "",
    "日程のご回答は、お送りしたメッセージの「日程を選ぶ」ボタンからお願いします。",
    "",
    "レッスンに関するご連絡・ご質問は、お手数ですが本アカウントまでお願いします。",
])


def _build(tenant: str) -> TenantConfig:
    legacy = tenant == DEFAULT_TENANT
    legacy_name = lambda name: name if legacy else ""
    locations = tuple(
        item.strip()
        for item in _env(tenant, "LOCATIONS", legacy_name("LOCATIONS")).split("|")
        if item.strip()
    )
    liff_id = _env(tenant, "LIFF_ID", legacy_name("LIFF_ID"))
    carte_liff_id = _env(tenant, "CARTE_LIFF_ID", legacy_name("CARTE_LIFF_ID"))
    return TenantConfig(
        name=tenant,
        channel_access_token=_env(tenant, "CHANNEL_ACCESS_TOKEN", legacy_name("LINE_CHANNEL_ACCESS_TOKEN")),
        channel_secret=_env(tenant, "CHANNEL_SECRET", legacy_name("LINE_CHANNEL_SECRET")),
        admin_token=_env(tenant, "ADMIN_TOKEN", legacy_name("ADMIN_TOKEN")),
        liff_id=liff_id,
        line_channel_id=_env(tenant, "LINE_CHANNEL_ID", legacy_name("LINE_CHANNEL_ID")),
        panel_name=_env(tenant, "PANEL_NAME", legacy_name("PANEL_NAME")) or f"Lesson {tenant.title()}",
        locations=locations,
        auto_reply=_env(tenant, "AUTO_REPLY", legacy_name("AUTO_REPLY")).strip() or DEFAULT_AUTO_REPLY,
        carte_liff_id=carte_liff_id,
        carte_enabled=_flag(_env(tenant, "CARTE_ENABLED", legacy_name("CARTE_ENABLED")), bool(carte_liff_id)),
        schedule_enabled=_flag(_env(tenant, "SCHEDULE_ENABLED", legacy_name("SCHEDULE_ENABLED")), True),
        storage_prefix=(
            _env(tenant, "STORAGE_PREFIX", legacy_name("STORAGE_PREFIX"))
            or ("" if tenant == "kansai" else f"{tenant}:")
        ),
    )


TENANTS = {name: _build(name) for name in TENANT_NAMES}


def get_tenant(name: str | None = None) -> TenantConfig:
    if name:
        return TENANTS[name]
    if has_request_context() and getattr(g, "tenant", None):
        return TENANTS[g.tenant]
    return TENANTS[DEFAULT_TENANT]


def validate_tenants() -> list[str]:
    errors = []
    for name, config in TENANTS.items():
        if not config.channel_access_token:
            errors.append(f"{name}: CHANNEL_ACCESS_TOKEN is missing")
        if not config.channel_secret:
            errors.append(f"{name}: CHANNEL_SECRET is missing")
    return errors
