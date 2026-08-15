import json
import base64
import hashlib
import hmac
from pathlib import Path

import pytest


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("KANSAI_CHANNEL_ACCESS_TOKEN", "kansai-token")
    monkeypatch.setenv("KANSAI_CHANNEL_SECRET", "kansai-secret")
    monkeypatch.setenv("KANSAI_ADMIN_TOKEN", "kansai-admin")
    monkeypatch.setenv("KANSAI_LIFF_ID", "kansai-liff")
    monkeypatch.setenv("KANSAI_LINE_CHANNEL_ID", "111")
    monkeypatch.setenv("KANSAI_PANEL_NAME", "関西")
    monkeypatch.setenv("KANSAI_SCHEDULE_ENABLED", "true")
    monkeypatch.setenv("KANSAI_CARTE_ENABLED", "false")
    monkeypatch.setenv("KANTO_CHANNEL_ACCESS_TOKEN", "kanto-token")
    monkeypatch.setenv("KANTO_CHANNEL_SECRET", "kanto-secret")
    monkeypatch.setenv("KANTO_ADMIN_TOKEN", "kanto-admin")
    monkeypatch.setenv("KANTO_CARTE_LIFF_ID", "kanto-carte")
    monkeypatch.setenv("KANTO_LINE_CHANNEL_ID", "222")
    monkeypatch.setenv("KANTO_PANEL_NAME", "関東")
    monkeypatch.setenv("KANTO_SCHEDULE_ENABLED", "false")
    monkeypatch.setenv("KANTO_CARTE_ENABLED", "true")

    import importlib
    import tenant_config
    import storage
    import app

    importlib.reload(tenant_config)
    importlib.reload(storage)
    importlib.reload(app)
    monkeypatch.setattr(storage, "BASE_DIR", tmp_path)
    return app.app.test_client(), storage, tmp_path


def test_feature_routes_are_tenant_scoped(client):
    http, _, _ = client
    assert http.get("/kansai/liff").status_code == 200
    assert http.get("/kansai/carte").status_code == 404
    assert http.get("/kanto/liff").status_code == 404
    assert http.get("/kanto/carte").status_code == 200


def test_carte_pages_embed_youtube_videos(client):
    http, _, _ = client
    student_html = http.get("/kanto/carte").get_data(as_text=True)

    import carte

    for html in (student_html, carte.ADMIN_HTML):
        assert 'id="videoDialog"' in html
        assert 'id="videoFrame"' in html
        assert "function youtubeId(url)" in html
        assert "www.youtube-nocookie.com/embed/" in html
        assert "playVideo(event,this.dataset.video)" in html
        assert "再生できない場合はYouTubeで開く" in html


def test_admin_page_keeps_tenant_in_links(client):
    http, _, _ = client
    response = http.get("/kansai/admin/panel?token=kansai-admin")
    assert response.status_code == 200
    assert "関西 管理画面" in response.get_data(as_text=True)
    assert "'/kansai/admin/" in response.get_data(as_text=True)


def test_storage_is_isolated_by_tenant(client):
    http, storage, tmp_path = client
    with http.application.test_request_context("/kansai/liff"):
        from flask import g
        g.tenant = "kansai"
        storage.save_json("members", [{"user_id": "west"}])
    with http.application.test_request_context("/kanto/carte"):
        from flask import g
        g.tenant = "kanto"
        storage.save_json("members", [{"user_id": "east"}])

    assert json.loads((Path(tmp_path) / "members.json").read_text()) == [{"user_id": "west"}]
    assert json.loads((Path(tmp_path) / "kanto_members.json").read_text()) == [{"user_id": "east"}]


def test_carte_storage_is_shared_across_tenants(client):
    http, storage, tmp_path = client
    with http.application.test_request_context("/kansai/carte"):
        from flask import g
        g.tenant = "kansai"
        storage.save_json("carte:progress", [{"user_id": "shared"}])
    with http.application.test_request_context("/kanto/carte"):
        from flask import g
        g.tenant = "kanto"
        assert storage.load_json("carte:progress") == [{"user_id": "shared"}]

    assert (Path(tmp_path) / "kanto_carte_progress.json").exists()


def test_unknown_tenant_is_rejected(client):
    http, _, _ = client
    assert http.post("/webhook/unknown").status_code == 404


@pytest.mark.parametrize(
    ("tenant", "secret"),
    [("kansai", "kansai-secret"), ("kanto", "kanto-secret")],
)
def test_each_webhook_uses_its_own_secret(client, tenant, secret):
    http, _, _ = client
    body = b'{"events":[]}'
    signature = base64.b64encode(
        hmac.new(secret.encode(), body, hashlib.sha256).digest()
    ).decode()
    assert http.post(
        f"/webhook/{tenant}",
        data=body,
        headers={"X-Line-Signature": signature, "Content-Type": "application/json"},
    ).status_code == 200

    wrong = base64.b64encode(
        hmac.new(b"wrong-secret", body, hashlib.sha256).digest()
    ).decode()
    assert http.post(
        f"/webhook/{tenant}",
        data=body,
        headers={"X-Line-Signature": wrong, "Content-Type": "application/json"},
    ).status_code == 400
