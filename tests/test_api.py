import os

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from cammon.api import create_app
from cammon.metadata import MetadataSynchronizer
from cammon.storage import LocalBackend
from tests.test_gateway import FIRST, create


@pytest.fixture
def client(gateway):
    with TestClient(create_app(gateway.settings, gateway)) as client:
        yield client


@pytest.fixture
def admin(client):
    response = client.post("/api/auth/login", json={"username": "admin", "password": "test-admin-password"},
                           headers={"X-CamMon-Request": "1"})
    assert response.status_code == 200
    client.headers.update({"X-CamMon-Request": "1"})
    return client


def test_auth_and_cross_origin_write_protection(client):
    assert client.get("/api/files").status_code == 401
    assert client.post("/api/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 403
    assert client.post("/api/auth/login", json={"username": "admin", "password": "wrong"},
                       headers={"X-CamMon-Request": "1"}).status_code == 401


def test_composite_filters_and_validation(admin, gateway, device):
    token = create(gateway, device)
    gateway.close(token)
    result = admin.get("/api/files?year=2025&month=10&day=2025-10-08&channel=00&kind=original").json()
    assert result["count"] == 1 and result["bytes"] == 10
    assert admin.get("/api/files?month=9").json()["count"] == 0
    assert admin.get("/api/files?channel=0").json()["count"] == 0
    assert admin.get("/api/files?month=13").status_code == 422
    assert admin.get("/api/files?day=2025-02-30").status_code == 422
    assert admin.get("/api/stats?device_id=" + device["id"]).json()["bytes"] == 10


@pytest.mark.parametrize("header,content,status", [
    (None, b"video-data", 200), ("bytes=0-4", b"video", 206), ("bytes=6-", b"data", 206),
    ("bytes=-4", b"data", 206), ("bytes=0-9999", b"video-data", 206),
    ("bytes=100-", b"", 416), ("bytes=0-2,4-6", b"", 416), ("bytes=-0", b"", 416),
])
def test_download_ranges(admin, gateway, device, header, content, status):
    token = create(gateway, device)
    gateway.close(token)
    headers = {"Range": header} if header else {}
    response = admin.get("/api/files/1/download", headers=headers)
    assert response.status_code == status
    if status != 416:
        assert response.content == content
        assert int(response.headers["content-length"]) == len(content)
    assert not gateway.handles


def test_head_empty_file_and_logout(admin, gateway, device):
    token = gateway.open(device["id"], FIRST, os.O_CREAT | os.O_RDWR)
    gateway.close(token)
    assert admin.get("/api/files/1/download").content == b""
    assert admin.head("/api/files/1/download").headers["content-length"] == "0"
    assert admin.post("/api/auth/logout").status_code == 200
    assert admin.get("/api/devices").status_code == 401


def test_storage_password_is_encrypted_and_destination_locked(admin, gateway, device, monkeypatch, tmp_path):
    monkeypatch.setattr("cammon.core.make_backend", lambda *args: LocalBackend(tmp_path / "target"))
    config = {"protocol": "smb", "address": "smb://example/share", "username": "writer", "password": "a-secret"}
    response = admin.put("/api/storage", json=config)
    assert response.status_code == 200
    assert "password" not in response.json()
    assert response.json()["has_password"]
    assert "a-secret" not in gateway.get_setting("storage")["password_encrypted"]
    token = create(gateway, device)
    gateway.close(token)
    assert admin.put("/api/storage", json={**config, "address": "smb://other/share"}).status_code == 422
    assert admin.put("/api/storage", json={**config, "password": None}).status_code == 200
    assert gateway.storage_config(reveal=True)["password"] == "a-secret"


def test_device_create_and_edit(admin):
    device = admin.post("/api/devices", json={"name": "后院", "retention_days": 7}).json()
    assert device["password"] and device["retention_days"] == 7
    assert "password" not in admin.get("/api/devices").json()[0]
    assert admin.patch(f"/api/devices/{device['id']}", json={"retention_days": 0}).status_code == 422
    assert admin.patch(f"/api/devices/{device['id']}", json={"enabled": False}).json()["enabled"] == 0


def test_metadata_health_has_no_connection_credentials(admin):
    metadata = admin.get("/api/status").json()["metadata"]
    assert metadata == {"configured": False, "connected": False, "schema": "cammon",
                        "pending_rows": 0, "last_sync": None, "error": None}


def test_postgres_outage_does_not_fail_health_or_configuration_api(admin, gateway, monkeypatch):
    gateway.settings.postgres_dsn = SecretStr("postgresql://secret:password@example/database")
    gateway.metadata = MetadataSynchronizer(gateway.db, gateway.settings)

    def disconnected():
        raise ConnectionError("PostgreSQL is unavailable")

    monkeypatch.setattr(gateway.metadata, "_connect", disconnected)
    assert not gateway.metadata.sync_once()
    assert admin.get("/healthz").status_code == 200
    response = admin.post("/api/devices", json={"name": "离线创建", "retention_days": 14})
    assert response.status_code == 201
    assert admin.get("/api/devices").json()[0]["name"] == "离线创建"
    status = admin.get("/api/status").json()["metadata"]
    assert status["error"] and status["pending_rows"] > 0
    assert "postgresql://" not in str(status)
