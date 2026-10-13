import os
import subprocess

import pytest
from fastapi.testclient import TestClient

from cammon.api import create_app
from cammon.importer import RecordingImporter
from cammon.media import MediaTools
from cammon.thumbnails import Thumbnails

NAME = "00_20251008080000_20251008080100.mp4"


@pytest.fixture
def admin(gateway):
    with TestClient(create_app(gateway.settings, gateway)) as client:
        client.headers.update({"X-CamMon-Request": "1"})
        assert client.post("/api/auth/login", json={
            "username": "admin", "password": "test-admin-password",
        }).status_code == 200
        yield client


def test_saved_video_thumbnail_is_the_first_frame_and_survives_cache_cleanup(gateway, device, tmp_path):
    source = tmp_path / "two-scenes.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=red:s=64x48:r=2:d=1",
        "-f", "lavfi", "-i", "color=blue:s=64x48:r=2:d=1", "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0",
        "-c:v", "libx264", "-threads", "1", str(source),
    ], check=True)
    token = gateway.open(device["id"], "day/" + NAME, os.O_CREAT | os.O_RDWR)
    gateway.write(token, source.read_bytes(), 0)
    gateway.close(token)
    gateway.seal_candidates(gateway.recording(1)["last_write"] + 1900)
    assert gateway.flush_one(1)
    row = gateway.recording(1)
    assert row["thumbnail_path"] and row["thumbnail_error"] is None
    assert not gateway.blob(1).exists() and gateway.cache_usage() == 0
    image = gateway.thumbnails.recording(1)
    assert image.startswith(b"\xff\xd8")
    decoded = subprocess.run([
        "ffmpeg", "-v", "error", "-i", str(gateway.backend._path(row["thumbnail_path"])),
        "-vf", "scale=1:1", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ], check=True, capture_output=True).stdout
    assert decoded[0] > 200 and decoded[1] < 40 and decoded[2] < 40
    # A fresh manager reads the persistent JPEG without any temporary video.
    assert Thumbnails(gateway).recording(1) == image


def test_thumbnail_failure_does_not_block_saved_recording(gateway, device):
    token = gateway.open(device["id"], "day/" + NAME, os.O_CREAT | os.O_RDWR)
    gateway.write(token, b"broken-video", 0)
    gateway.close(token)
    gateway.seal_candidates(gateway.recording(1)["last_write"] + 1900)
    assert gateway.flush_one(1)
    row = gateway.recording(1)
    assert row["state"] == "stored" and row["error"] is None
    assert row["thumbnail_path"] is None and row["thumbnail_error"]
    assert gateway.backend._path(row["remote_path"]).read_bytes() == b"broken-video"


def test_preview_pages_include_files_beyond_old_sample_limit_and_generate_only_requested_frame(
        admin, gateway, device, videos, monkeypatch):
    folder = gateway.backend.root / "history"
    folder.mkdir()
    data = videos(2)
    for channel in range(101):
        (folder / f"{channel:03d}_20251008080000_20251008080100.mp4").write_bytes(data)
    calls = []
    original = MediaTools._run_process

    def record(self, command, *args, **kwargs):
        calls.append(command)
        return original(self, command, *args, **kwargs)

    monkeypatch.setattr(MediaTools, "_run_process", record)
    preview = admin.post("/api/import/preview", json={"folder": str(folder), "device_id": device["id"]}).json()
    assert preview["total"] == 101 and len(preview["items"]) == 25 and calls == []
    preview_id = preview["preview_id"]
    last = admin.get(f"/api/import/previews/{preview_id}?offset=100&limit=25").json()
    assert last["total"] == 101 and len(last["items"]) == 1
    assert last["items"][0]["path"].startswith("100_") and calls == []
    response = admin.get(f"/api/import/previews/{preview_id}/thumbnail?index=100")
    assert response.status_code == 200 and response.headers["content-type"] == "image/jpeg"
    assert response.content.startswith(b"\xff\xd8") and len(calls) == 1
    assert len(list((gateway.backend.root / "thumbnails").rglob("*.jpg"))) == 1
    assert admin.get(f"/api/import/previews/{preview_id}/thumbnail?index=100").content == response.content
    assert len(calls) == 1 and gateway.query_files({})["count"] == 0
    assert admin.get(f"/api/import/previews/{preview_id}?offset=-1").status_code == 422
    assert admin.get(f"/api/import/previews/{preview_id}/thumbnail?index=101").status_code == 404
    assert admin.get(f"/api/import/previews/{preview_id}/thumbnail?index=-1").status_code == 422


def test_import_reuses_preview_image_and_records_unpreviewed_images(admin, gateway, device, videos, monkeypatch):
    folder = gateway.backend.root / "history"
    folder.mkdir()
    data = videos(2)
    (folder / NAME).write_bytes(data)
    other = "01_20251008080000_20251008080100.mp4"
    (folder / other).write_bytes(data)
    preview = admin.post("/api/import/preview", json={"folder": str(folder), "device_id": device["id"]}).json()
    key = preview["items"][0]["thumbnail_key"]
    image = admin.get(f"/api/import/previews/{preview['preview_id']}/thumbnail?index=0").content
    calls = []
    original = MediaTools._run_process

    def record(self, command, *args, **kwargs):
        calls.append(command)
        return original(self, command, *args, **kwargs)

    monkeypatch.setattr(MediaTools, "_run_process", record)
    assert RecordingImporter(gateway).scan(device["id"], str(folder), commit=True)["imported"] == 2
    rows = gateway.query_files({})["items"]
    row = next(row for row in rows if row["channel"] == "00")
    assert row["thumbnail_path"] == key and len(calls) == 1
    assert all(row["thumbnail_path"] for row in rows)
    assert admin.get(f"/api/files/{row['id']}/thumbnail").content == image
    assert gateway.db.metadata_pending("recordings", [row["id"]])
    assert (folder / NAME).read_bytes() == data and gateway.cache_usage() == 0


@pytest.mark.parametrize("change", ["content", "symlink"])
def test_preview_rejects_changed_video_and_symlink(admin, gateway, device, videos, tmp_path, change):
    folder = gateway.backend.root / "history"
    folder.mkdir()
    source = folder / NAME
    source.write_bytes(videos(2))
    preview = admin.post("/api/import/preview", json={"folder": str(folder), "device_id": device["id"]}).json()
    if change == "content":
        source.write_bytes(b"changed-video")
    else:
        target = tmp_path / "outside.mp4"
        target.write_bytes(source.read_bytes())
        source.unlink()
        source.symlink_to(target)
    response = admin.get(f"/api/import/previews/{preview['preview_id']}/thumbnail?index=0")
    assert response.status_code == 422 and not (gateway.backend.root / "thumbnails").exists()


def test_thumbnail_and_preview_endpoints_require_login(admin, gateway):
    admin.post("/api/auth/logout")
    assert admin.get("/api/files/1/thumbnail").status_code == 401
    assert admin.get("/api/import/previews/missing").status_code == 401
    assert admin.get("/api/import/previews/missing/thumbnail?index=0").status_code == 401


def test_linked_copy_uses_preview_image_for_existing_recording_without_thumbnail(admin, gateway, device, videos):
    folder = gateway.backend.root / "history"
    folder.mkdir()
    data = videos(2)
    (folder / NAME).write_bytes(data)
    assert RecordingImporter(gateway).scan(device["id"], str(folder), commit=True)["imported"] == 1
    # Model a video indexed by a version without thumbnail support.
    gateway.db.execute("UPDATE recordings SET thumbnail_path=NULL,thumbnail_error=NULL WHERE id=1")
    copies = gateway.backend.root / "copies"
    copies.mkdir()
    (copies / NAME).write_bytes(data)
    preview = admin.post("/api/import/preview", json={"folder": str(copies), "device_id": device["id"]}).json()
    assert preview["copies"] == 1
    response = admin.get(f"/api/import/previews/{preview['preview_id']}/thumbnail?index=0")
    assert response.status_code == 200
    report = RecordingImporter(gateway).scan(device["id"], str(copies), commit=True)
    assert report["copies"] == 1 and report["imported"] == 0
    assert gateway.recording(1)["thumbnail_path"] == preview["items"][0]["thumbnail_key"]
    assert gateway.thumbnails.recording(1) == response.content
