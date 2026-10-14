import os
import threading
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from cammon.api import create_app
from cammon.importer import RecordingImporter
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
@pytest.mark.parametrize("endpoint,disposition", [("download", "attachment"), ("stream", "inline")])
def test_recording_ranges(admin, gateway, device, header, content, status, endpoint, disposition):
    token = create(gateway, device)
    gateway.close(token)
    headers = {"Range": header} if header else {}
    response = admin.get(f"/api/files/1/{endpoint}", headers=headers)
    assert response.status_code == status
    if status != 416:
        assert response.content == content
        assert int(response.headers["content-length"]) == len(content)
        assert response.headers["content-type"] == "video/mp4"
        assert response.headers["content-disposition"].startswith(disposition + ";")
        assert response.headers["accept-ranges"] == "bytes"
        assert response.headers["cache-control"] == "no-store"
        if status == 206:
            assert response.headers["content-range"].endswith("/10")
    assert not gateway.handles


@pytest.mark.parametrize("endpoint", ["download", "stream"])
def test_head_empty_file_and_logout(admin, gateway, device, endpoint):
    token = gateway.open(device["id"], FIRST, os.O_CREAT | os.O_RDWR)
    gateway.close(token)
    assert admin.get(f"/api/files/1/{endpoint}").content == b""
    assert admin.head(f"/api/files/1/{endpoint}").headers["content-length"] == "0"
    assert not gateway.handles
    assert admin.post("/api/auth/logout").status_code == 200
    assert admin.get("/api/devices").status_code == 401
    assert admin.get(f"/api/files/1/{endpoint}").status_code == 401
    assert admin.head(f"/api/files/1/{endpoint}").status_code == 401


@pytest.mark.parametrize("endpoint", ["download", "stream"])
def test_virtual_original_is_excluded_from_storage_totals_and_content(admin, gateway, device, endpoint):
    token = create(gateway, device)
    gateway.close(token)
    gateway.db.execute("UPDATE recordings SET state='virtual',duration=end-start WHERE id=1")
    gateway.blob(1).unlink()
    assert gateway.stat(device["id"], FIRST)["size"] == 10
    assert admin.get("/api/files").json()["items"] == []
    assert admin.get("/api/stats").json()["bytes"] == 0
    for method in (admin.get, admin.head):
        assert method(f"/api/files/1/{endpoint}").status_code == 410
        assert method(f"/api/files/1/{endpoint}", headers={"Range": "bytes=0-4"}).status_code == 410
    assert not gateway.handles


@pytest.mark.parametrize("kind", ["original", "archive"])
def test_stream_stored_recording_and_head_seek(admin, gateway, device, kind):
    token = create(gateway, device)
    gateway.close(token)
    gateway.seal_candidates(gateway.recording(1)["last_write"] + 1900)
    assert gateway.flush_one(1)
    gateway.db.execute("UPDATE recordings SET kind=? WHERE id=1", (kind,))
    assert not gateway.blob(1).exists()
    response = admin.head("/api/files/1/stream", headers={"Range": "bytes=6-"})
    assert response.status_code == 206 and not response.content
    assert response.headers["content-length"] == "4"
    assert response.headers["content-range"] == "bytes 6-9/10"
    assert not gateway.handles
    response = admin.get("/api/files/1/stream", headers={"Range": "bytes=6-"})
    assert response.status_code == 206 and response.content == b"data"
    assert not gateway.handles
    assert admin.get("/api/files/9999/stream").status_code == 404


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


def test_local_disk_probe_save_publish_download_and_destination_lock(admin, gateway, device, tmp_path):
    root = tmp_path / "mounted-disk"
    config = {"address": str(root)}  # The default storage protocol is local disk.
    response = admin.post("/api/storage/test", json=config)
    assert response.status_code == 200 and response.json()["ok"]
    assert list(root.iterdir()) == []
    assert gateway.storage_config() is None  # Probing does not save configuration.

    response = admin.put("/api/storage", json=config)
    assert response.status_code == 200
    assert response.json()["protocol"] == "local" and not response.json()["has_password"]
    assert admin.get("/api/storage").json()["address"] == str(root)
    token = create(gateway, device)
    gateway.close(token)
    gateway.seal_candidates(gateway.recording(1)["last_write"] + 1900)
    assert gateway.flush_one(1), gateway.recording(1)["error"]
    row = gateway.recording(1)
    assert row["state"] == "stored"
    assert (root / row["remote_path"]).read_bytes() == b"video-data"
    assert not gateway.blob(1).exists()
    response = admin.get("/api/files/1/download", headers={"Range": "bytes=6-"})
    assert response.status_code == 206 and response.content == b"data"
    other_root = tmp_path / "other-disk"
    assert admin.put("/api/storage", json={"address": str(other_root)}).status_code == 422
    assert not other_root.exists()
    assert admin.put("/api/storage", json={"protocol": "nfs", "address": "nfs://example/export"}).status_code == 422
    assert admin.put("/api/storage", json=config).status_code == 200


@pytest.mark.parametrize("address", ["", "relative/path", "/data/../other", "smb://example/share", "/data\x00"])
def test_local_disk_rejects_invalid_addresses(admin, address):
    response = admin.put("/api/storage", json={"protocol": "local", "address": address})
    assert response.status_code == 422


def test_device_create_and_edit(admin):
    device = admin.post("/api/devices", json={"name": "后院", "retention_days": 7}).json()
    assert device["password"] and device["retention_days"] == 7
    assert "password" not in admin.get("/api/devices").json()[0]
    assert admin.patch(f"/api/devices/{device['id']}", json={"retention_days": 0}).status_code == 422
    assert admin.patch(f"/api/devices/{device['id']}", json={"enabled": False}).json()["enabled"] == 0


def test_import_browse_preview_commit_and_download(admin, gateway, device):
    root = gateway.backend.root
    folder = root / "history"
    folder.mkdir()
    name = "00_20251008080000_20251008080100.mp4"
    (folder / name).write_bytes(b"old-recording")
    listing = admin.get("/api/import/folders").json()
    assert listing["root"] == str(root) and listing["folders"][0]["name"] == "history"
    body = {"folder": str(folder), "device_id": device["id"]}
    preview = admin.post("/api/import/preview", json=body)
    assert preview.status_code == 200 and preview.json()["importable"] == 1
    assert admin.get("/api/files").json()["count"] == 0
    result = admin.post("/api/import", json=body)
    assert result.status_code == 202
    assert wait_for_import(admin, result.json()["id"])["report"]["imported"] == 1
    row = admin.get("/api/files").json()["items"][0]
    assert row["day"] == "2025-10-08" and row["channel"] == "00"
    assert admin.get(f"/api/files/{row['id']}/download").content == b"old-recording"
    repeated = admin.post("/api/import", json=body).json()
    assert wait_for_import(admin, repeated["id"])["report"]["duplicates"] == 1
    assert admin.get("/api/import/folders", params={"path": str(root.parent)}).status_code == 422
    assert admin.post("/api/import", json={**body, "device_id": "missing"}).status_code == 409
    assert admin.post("/api/import", json={**body, "folder": str(root / "missing")}).status_code == 404
    assert admin.post("/api/import", json={"device_id": device["id"]}).status_code == 422


def test_import_requires_authentication_and_write_header(client, gateway, device):
    body = {"folder": str(gateway.backend.root), "device_id": device["id"]}
    assert client.get("/api/import/folders").status_code == 401
    assert client.get("/api/storage/ownership", params={"path": str(gateway.backend.root)}).status_code == 401
    assert client.get("/api/import/jobs").status_code == 401
    assert client.get("/api/import/jobs/missing").status_code == 401
    assert client.post("/api/import", json=body).status_code == 403
    assert client.post("/api/import", json=body, headers={"X-CamMon-Request": "1"}).status_code == 401


def test_path_ownership_query_browse_and_import_conflicts(admin, gateway, device):
    root = gateway.backend.root
    folder = root / "history"
    child = folder / "nested"
    child.mkdir(parents=True)
    source = child / FIRST.split("/")[-1]
    source.write_bytes(b"history")
    body = {"device_id": device["id"], "folder": str(folder)}
    def query(path):
        return admin.get("/api/storage/ownership", params={"path": str(path)})

    assert query(folder).json()["status"] == "unassigned"
    assert admin.post("/api/import/preview", json=body).status_code == 200
    assert query(folder).json()["status"] == "unassigned"
    job = admin.post("/api/import", json=body)
    assert job.status_code == 202
    assert query(folder).json()["owners"][0]["device_id"] == device["id"]
    assert wait_for_import(admin, job.json()["id"])["report"]["imported"] == 1
    assert query(source).json()["recording_ids"] == [1]
    folders = admin.get("/api/import/folders").json()["folders"]
    assert folders[0]["ownership"]["owners"][0]["device_name"] == device["name"]
    other = gateway.create_device("另一台摄像机")
    for path in (folder, child):
        for endpoint in ("/api/import/preview", "/api/import"):
            response = admin.post(endpoint, json={**body, "device_id": other["id"],
                                                 "folder": str(path), "recursive": False})
            assert response.status_code == 409 and device["name"] in response.json()["detail"]
    assert gateway.query_files({"device_id": other["id"]})["count"] == 0
    assert query(root.parent).status_code == 422
    assert admin.get("/api/storage/ownership").status_code == 422
    assert admin.post("/api/import", json={**body, "folder": str(root)}).status_code == 422


def test_path_ownership_query_includes_camera_uploads(admin, gateway, device):
    token = create(gateway, device)
    gateway.close(token)
    gateway.seal_candidates(gateway.recording(1)["last_write"] + 1900)
    assert gateway.flush_one(1)
    row = gateway.recording(1)
    response = admin.get("/api/storage/ownership", params={"path": str(gateway.backend.root / row["remote_path"])})
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "owned" and result["exists"] and result["recording_ids"] == [1]
    assert result["owners"] == [{"device_id": device["id"], "device_name": device["name"]}]


def test_import_only_duplicate_camera_video_links_copy(admin, gateway, device):
    token = create(gateway, device)
    gateway.close(token)
    gateway.seal_candidates(gateway.recording(1)["last_write"] + 1900)
    assert gateway.flush_one(1)
    folder = gateway.backend.root / "history"
    folder.mkdir()
    source = folder / FIRST.split("/")[-1]
    source.write_bytes(b"video-data")
    body = {"device_id": device["id"], "folder": str(folder)}
    preview = admin.post("/api/import/preview", json=body).json()
    assert preview["importable"] == 0 and preview["copies"] == 1
    job = admin.post("/api/import", json=body).json()
    report = wait_for_import(admin, job["id"])["report"]
    assert report["copies"] == 1 and report["imported"] == 0
    assert source.exists()
    assert admin.get("/api/files").json()["count"] == 1


def wait_for_import(client, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/import/jobs/{job_id}")
        assert response.status_code == 200
        job = response.json()
        if job["status"] != "running":
            return job
        time.sleep(0.01)
    pytest.fail("Import task did not finish")


def test_background_import_progress_reconnect_and_duplicate_submission(admin, gateway, device, monkeypatch):
    folder = gateway.backend.root / "history"
    folder.mkdir()
    names = ["00_20251008080000_20251008080100.mp4", "01_20251008080000_20251008080100.mp4"]
    for name in names:
        (folder / name).write_bytes(b"historical-video")
    entered, release = threading.Event(), threading.Event()
    original = RecordingImporter.candidate

    def candidate(self, source):
        if source.name == names[1]:
            entered.set()
            assert release.wait(5), "Test did not release the slow import"
        return original(self, source)

    monkeypatch.setattr(RecordingImporter, "candidate", candidate)
    body = {"folder": str(folder), "device_id": device["id"]}
    assert admin.get("/api/import/jobs").json() == []
    assert admin.get("/api/import/jobs/missing").status_code == 404
    try:
        response = admin.post("/api/import", json=body)
        assert response.status_code == 202
        job_id = response.json()["id"]
        assert entered.wait(5)
        progress = admin.get(f"/api/import/jobs/{job_id}").json()
        assert progress["status"] == "running" and progress["total_files"] == 2
        assert progress["report"]["total"] == 1 and progress["report"]["imported"] == 1
        assert progress["current_path"] == names[1]
        assert admin.post("/api/import", json=body).json()["id"] == job_id
        assert admin.post("/api/import", json={**body, "recursive": False}).status_code == 409
        # A new browser connection shares the server's task, without restarting its lifespan.
        reconnected = TestClient(admin.app, cookies=admin.cookies)
        try:
            assert reconnected.get("/api/import/jobs").json()[0] == progress
            release.set()
            finished = wait_for_import(reconnected, job_id)
            assert finished["status"] == "completed" and finished["finished_at"]
            assert finished["report"]["imported"] == 2 and finished["current_path"] is None
            assert reconnected.get("/api/import/jobs").json()[0] == finished
        finally:
            reconnected.close()
        assert gateway.query_files({})["count"] == 2
    finally:
        release.set()


def test_background_import_failure_retains_partial_progress_and_can_retry(admin, gateway, device, monkeypatch):
    folder = gateway.backend.root / "history"
    folder.mkdir()
    for channel in ("00", "01"):
        (folder / f"{channel}_20251008080000_20251008080100.mp4").write_bytes(b"historical-video")
    original = RecordingImporter.candidate

    def candidate(self, source):
        if source.name.startswith("01_"):
            raise RuntimeError("unexpected scan error")
        return original(self, source)

    monkeypatch.setattr(RecordingImporter, "candidate", candidate)
    body = {"folder": str(folder), "device_id": device["id"]}
    started = admin.post("/api/import", json=body).json()
    failed = wait_for_import(admin, started["id"])
    assert failed["status"] == "failed" and failed["error"] == "unexpected scan error"
    assert failed["report"]["total"] == 1 and failed["report"]["imported"] == 1
    assert gateway.query_files({})["count"] == 1
    monkeypatch.setattr(RecordingImporter, "candidate", original)
    retried = admin.post("/api/import", json=body).json()
    finished = wait_for_import(admin, retried["id"])
    assert finished["status"] == "completed"
    assert finished["report"]["imported"] == 1 and finished["report"]["duplicates"] == 1
    assert [job["id"] for job in admin.get("/api/import/jobs").json()] == [retried["id"], started["id"]]


def test_background_import_history_is_bounded_and_empty_folder_completes(admin, gateway, device):
    admin.app.state.imports.history_limit = 2
    folder = gateway.backend.root / "empty"
    folder.mkdir()
    ids = []
    for _ in range(3):
        job = admin.post("/api/import", json={"device_id": device["id"], "folder": str(folder)}).json()
        ids.append(job["id"])
        finished = wait_for_import(admin, job["id"])
        assert finished["status"] == "completed" and finished["total_files"] == 0
        assert finished["report"]["total"] == 0
    assert [job["id"] for job in admin.get("/api/import/jobs").json()] == ids[:0:-1]
    assert admin.get(f"/api/import/jobs/{ids[0]}").status_code == 404


def test_background_import_stop_waits_for_worker_and_retains_committed_files(admin, gateway, device, monkeypatch):
    folder = gateway.backend.root / "history"
    folder.mkdir()
    for channel in ("00", "01"):
        (folder / f"{channel}_20251008080000_20251008080100.mp4").write_bytes(b"historical-video")
    entered, release = threading.Event(), threading.Event()
    original = RecordingImporter.candidate

    def candidate(self, source):
        if source.name.startswith("01_"):
            entered.set()
            assert release.wait(5)
        return original(self, source)

    monkeypatch.setattr(RecordingImporter, "candidate", candidate)
    manager = admin.app.state.imports
    job = admin.post("/api/import", json={"device_id": device["id"], "folder": str(folder)}).json()
    stopping = threading.Thread(target=manager.stop)
    try:
        assert entered.wait(5)
        stopping.start()
        assert manager.stopping.wait(5)
        assert stopping.is_alive()
    finally:
        release.set()
        if stopping.ident is not None:
            stopping.join(timeout=5)
    assert not stopping.is_alive() and not manager.thread.is_alive()
    stopped = manager.get(job["id"])
    assert stopped["status"] == "failed" and "导入已中断" in stopped["error"]
    assert stopped["report"]["imported"] == gateway.query_files({})["count"] == 2
    assert admin.post("/api/import", json={"device_id": device["id"], "folder": str(folder)}).status_code == 409


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
