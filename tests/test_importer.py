import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

from cammon.importer import COPY_PREFIX, RecordingImporter, copy_key
from cammon.naming import TZ
from cammon.storage import Backend

FIRST = "00_20251008080000_20251008080100.mp4"
SECOND = "0_20251009080000_20251009080100.mp4"


@pytest.fixture
def history(gateway):
    folder = gateway.backend.root / "旧录像"
    folder.mkdir()
    (folder / FIRST).write_bytes(b"historical-video")
    nested = folder / "2025" / "10" / "09"
    nested.mkdir(parents=True)
    (nested / SECOND).write_bytes(b"nested-video")
    (folder / "notes.txt").write_text("not a video")
    return folder


def test_minute_history_preview_and_import_preserve_source_and_use_actual_duration(gateway, device, videos):
    folder = gateway.backend.root / "history" / "xiaomi_camera_videos" / "788b2a487711" / "2025072621"
    folder.mkdir(parents=True)
    source = folder / "30M33S_1753536633.mp4"
    data = videos(12.5, "10")
    source.write_bytes(data)
    importer = RecordingImporter(gateway)
    preview = importer.scan(device["id"], str(folder.parents[2]))
    assert preview["importable"] == 1 and preview["items"][0]["channel"] == "00"
    assert gateway.query_files({})["count"] == 0 and source.read_bytes() == data
    assert importer.scan(device["id"], str(folder.parents[2]), commit=True)["imported"] == 1
    row = gateway.query_files({})["items"][0]
    assert row["start"] == 1753536633 and row["end"] == 1753536645.5 and row["duration"] == 12.5
    assert row["day"] == "2025-07-26" and row["channel"] == "00" and row["state"] == "stored"
    assert gateway.backend._path(row["remote_path"]) == source and source.read_bytes() == data
    assert gateway.cache_usage() == 0
    assert importer.scan(device["id"], str(folder), commit=True)["duplicates"] == 1
    reader = gateway.open(device["id"], row["path"], os.O_RDONLY)
    assert gateway.read(reader, len(data), 0) == data
    gateway.close(reader)


def test_minute_history_invalid_names_hours_empty_and_corrupt_files_are_skipped(gateway, device, videos):
    folder = gateway.backend.root / "history"
    folder.mkdir()
    data = videos(5)
    (folder / "30M33S_1753536633.mp4").write_bytes(data)
    (folder / FIRST).write_bytes(b"supported old format")
    (folder / "31M33S_1753536633.mp4").write_bytes(data)
    (folder / "32M33S_1753536753.mp4").write_bytes(b"broken video")
    (folder / "33M33S_1753536813.mp4").touch()
    wrong_hour = folder / "2025072613"
    wrong_hour.mkdir()
    (wrong_hour / "30M33S_1753536633.mp4").write_bytes(data)
    report = RecordingImporter(gateway).scan(device["id"], str(folder), commit=True)
    assert report["imported"] == 2 and report["skipped"] == 4 and report["failed"] == 0
    assert all(item["reason"] for item in report["items"] if item["status"] == "skipped")
    assert all(source.exists() for source in folder.rglob("*.mp4"))


def test_minute_history_links_identical_live_recording_using_probed_end_time(gateway, device, videos):
    name = "30M33S_1753536633.mp4"
    data = videos(12.5, "10")
    writer = gateway.open(device["id"], "day/" + name, os.O_CREAT | os.O_RDWR)
    gateway.write(writer, data, 0)
    gateway.close(writer)
    gateway.seal_candidates(gateway.recording(1)["last_write"] + 1900)
    assert gateway.flush_one(1)
    folder = gateway.backend.root / "history"
    folder.mkdir()
    (folder / name).write_bytes(data)
    report = RecordingImporter(gateway).scan(device["id"], str(folder), commit=True)
    assert report["copies"] == 1 and report["duplicates"] == 1 and report["imported"] == 0
    assert gateway.query_files({})["count"] == 1
    assert gateway.get_setting(copy_key("history/" + name))["recording_id"] == 1


def test_minute_history_cannot_duplicate_camera_segment_with_temporary_end_time(gateway, device, videos):
    name = "30M33S_1753536633.mp4"
    data = videos(12.5, "10")
    writer = gateway.open(device["id"], "day/" + name, os.O_CREAT | os.O_RDWR)
    gateway.write(writer, data, 0)
    folder = gateway.backend.root / "history"
    folder.mkdir()
    (folder / name).write_bytes(data)
    importer = RecordingImporter(gateway)
    for state in ("cached", "pending"):
        report = importer.scan(device["id"], str(folder), commit=True)
        assert gateway.recording(1)["state"] == state and gateway.recording(1)["duration"] is None
        assert report["imported"] == 0 and report["copies"] == 0 and report["skipped"] == 1
        assert "尚未封存" in report["items"][0]["reason"]
        assert gateway.query_files({})["count"] == 1 and (folder / name).exists()
        if state == "cached":
            gateway.close(writer)
            gateway.seal_candidates(gateway.recording(1)["last_write"] + 1900)
    assert gateway.flush_one(1)
    report = importer.scan(device["id"], str(folder), commit=True)
    assert report["copies"] == 1 and report["imported"] == 0 and gateway.query_files({})["count"] == 1


def test_minute_history_same_name_with_different_duration_is_preserved_as_conflict(gateway, device, videos):
    name = "30M33S_1753536633.mp4"
    writer = gateway.open(device["id"], "day/" + name, os.O_CREAT | os.O_RDWR)
    gateway.write(writer, videos(12.5, "10"), 0)
    gateway.close(writer)
    gateway.seal_candidates(gateway.recording(1)["last_write"] + 1900)
    assert gateway.flush_one(1)
    folder = gateway.backend.root / "history"
    folder.mkdir()
    different = videos(5)
    (folder / name).write_bytes(different)
    report = RecordingImporter(gateway).scan(device["id"], str(folder), commit=True)
    assert report["skipped"] == 1 and report["imported"] == 0 and report["copies"] == 0
    assert "内容不同" in report["items"][0]["reason"]
    assert (folder / name).read_bytes() == different and gateway.query_files({})["count"] == 1


def test_preview_then_adopt_nested_recordings_without_copying(gateway, device, history):
    importer = RecordingImporter(gateway)
    preview = importer.scan(device["id"], str(history))
    assert preview["total"] == 3 and preview["importable"] == 2 and preview["skipped"] == 1
    assert gateway.query_files({})["count"] == 0
    report = importer.scan(device["id"], str(history), commit=True)
    assert report["imported"] == 2 and report["bytes"] == len(b"historical-videonested-video")
    assert gateway.query_files({})["count"] == 2
    assert gateway.cache_usage() == 0 and list(gateway.blobs.iterdir()) == []
    rows = gateway.query_files({"kind": "original"})["items"]
    assert {row["channel"] for row in rows} == {"00", "0"}
    for row in rows:
        assert row["state"] == "stored" and len(row["path"].split("/")) == 3
        assert (gateway.backend.root / row["remote_path"]).is_file()
        handle = gateway.open_recording(row["id"])
        try:
            assert gateway.read(handle, 1024, 0) in (b"historical-video", b"nested-video")
        finally:
            gateway.close(handle)
        assert gateway.db.metadata_pending("recordings", [row["id"]])
    assert not (gateway.backend.root / "originals").exists()


def test_repeat_overlapping_folder_and_cross_device_import_are_idempotent(gateway, device, history):
    importer = RecordingImporter(gateway)
    assert importer.scan(device["id"], str(history), commit=True)["imported"] == 2
    repeated = importer.scan(device["id"], str(history), commit=True)
    assert repeated["duplicates"] == 2 and repeated["imported"] == 0
    nested = importer.scan(device["id"], str(history / "2025"), commit=True)
    assert nested["duplicates"] == 1
    other = gateway.create_device("另一台设备")
    with pytest.raises(ValueError, match="已归属摄像机"):
        importer.scan(other["id"], str(history), commit=True)
    assert gateway.query_files({})["count"] == 2


def test_concurrent_import_does_not_duplicate_source(gateway, device, history):
    def run():
        return RecordingImporter(gateway).scan(device["id"], str(history), commit=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reports = list(pool.map(lambda _: run(), range(2)))
    assert sum(report["imported"] for report in reports) == 2
    assert gateway.query_files({})["count"] == 2


def test_nonrecursive_scan_and_disabled_device(gateway, device, history):
    gateway.update_device(device["id"], {"enabled": False})
    report = RecordingImporter(gateway).scan(device["id"], str(history), recursive=False, commit=True)
    assert report["total"] == 2 and report["imported"] == 1


def test_folder_browser_and_scan_exclude_managed_files_and_symlinks(gateway, device, history, tmp_path):
    root = gateway.backend.root
    for name in ("originals", "archives"):
        directory = root / name
        directory.mkdir()
        (directory / FIRST).write_bytes(b"already managed")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / FIRST).write_bytes(b"outside")
    (root / "outside-link").symlink_to(outside, target_is_directory=True)
    (history / SECOND).symlink_to(outside / FIRST)
    importer = RecordingImporter(gateway)
    listing = importer.folders()
    assert listing["parent"] is None
    assert [child["name"] for child in listing["folders"]] == ["旧录像"]
    assert importer.folders(str(history))["parent"] == str(root)
    with pytest.raises(ValueError, match="公共容器"):
        importer.scan(device["id"], str(root), commit=True)
    report = importer.scan(device["id"], str(history), commit=True)
    assert report["imported"] == 2 and report["skipped"] == 2
    for path in (outside, root / "outside-link", root / "originals", root / "archives"):
        with pytest.raises(ValueError):
            importer.scan(device["id"], str(path), commit=True)


@pytest.mark.parametrize("folder", ["relative", "/data/../outside", "/data\x00"])
def test_invalid_folder_paths(gateway, device, folder):
    with pytest.raises(ValueError):
        RecordingImporter(gateway).scan(device["id"], folder)


def test_invalid_timestamps_empty_files_and_zero_duration_are_reported(gateway, device, history):
    (history / "00_20250230080000_20250230080100.mp4").write_bytes(b"invalid date")
    (history / "00_20251009080000_20251008080100.mp4").write_bytes(b"backwards")
    (history / "00_20251008080000_20251008080000.mp4").write_bytes(b"zero duration")
    (history / "01_20251008080000_20251008080100.mp4").touch()
    report = RecordingImporter(gateway).scan(device["id"], str(history), commit=True)
    assert report["imported"] == 2 and report["skipped"] == 5
    assert all(item["reason"] for item in report["items"] if item["status"] == "skipped")


def test_same_name_with_different_contents_is_preserved_and_reported(gateway, device, history):
    (history / "2025" / FIRST).write_bytes(b"another source")
    report = RecordingImporter(gateway).scan(device["id"], str(history), commit=True)
    assert report["imported"] == 2 and report["skipped"] == 2
    assert "内容不同" in next(item for item in report["items"] if item["path"] == "2025/" + FIRST)["reason"]
    assert (history / "2025" / FIRST).read_bytes() == b"another source"
    assert len({row["path"] for row in gateway.query_files({})["items"]}) == 2


def test_same_camera_filename_with_identical_contents_links_copy_without_second_recording(gateway, device, history):
    from tests.test_gateway import create

    token = create(gateway, device, data=b"historical-video")
    gateway.close(token)
    gateway.seal_candidates(gateway.recording(1)["last_write"] + 1900)
    assert gateway.flush_one(1)
    importer = RecordingImporter(gateway)
    preview = importer.scan(device["id"], str(history), recursive=False)
    assert preview["duplicates"] == 1 and preview["copies"] == 1 and preview["importable"] == 0
    assert not gateway.get_setting(copy_key("旧录像/" + FIRST))
    report = importer.scan(device["id"], str(history), recursive=False, commit=True)
    assert report["duplicates"] == 1 and report["copies"] == 1 and report["imported"] == 0
    copy = gateway.get_setting(copy_key("旧录像/" + FIRST))
    assert copy["recording_id"] == 1 and copy["size"] == len(b"historical-video")
    assert gateway.query_files({})["count"] == 1 and (history / FIRST).exists()
    repeated = importer.scan(device["id"], str(history), recursive=False, commit=True)
    assert repeated["duplicates"] == 1 and repeated["copies"] == 0


def test_camera_filename_conflict_never_links_or_deletes_different_content(gateway, device, history):
    from tests.test_gateway import create

    token = create(gateway, device, data=b"different-video!")
    gateway.close(token)
    gateway.seal_candidates(gateway.recording(1)["last_write"] + 1900)
    assert gateway.flush_one(1)
    report = RecordingImporter(gateway).scan(device["id"], str(history), recursive=False, commit=True)
    assert report["imported"] == 0 and report["copies"] == 0 and report["skipped"] == 2
    assert (history / FIRST).read_bytes() == b"historical-video"
    assert gateway.query_files({})["count"] == 1
    assert not gateway.db.all("SELECT key FROM settings WHERE key LIKE ?", (COPY_PREFIX + "%",))


def test_writing_camera_duplicate_is_kept_until_it_is_sealed(gateway, device, history):
    from tests.test_gateway import create

    token = create(gateway, device, data=b"historical-video")
    report = RecordingImporter(gateway).scan(device["id"], str(history), recursive=False, commit=True)
    assert report["copies"] == 0 and report["imported"] == 0
    assert "尚未封存" in next(item for item in report["items"] if item["path"] == FIRST)["reason"]
    assert (history / FIRST).exists()
    gateway.close(token)


def test_failed_projection_keeps_source_and_can_retry(gateway, device, history, monkeypatch):
    real_mirror = gateway._mirror_file
    monkeypatch.setattr(gateway, "_mirror_file", lambda *args: (_ for _ in ()).throw(OSError("disk error")))
    report = RecordingImporter(gateway).scan(device["id"], str(history), commit=True)
    assert report["failed"] == 2 and report["imported"] == 0
    assert gateway.query_files({})["count"] == 0
    assert (history / FIRST).read_bytes() == b"historical-video"
    monkeypatch.setattr(gateway, "_mirror_file", real_mirror)
    assert RecordingImporter(gateway).scan(device["id"], str(history), commit=True)["imported"] == 2


def test_storage_change_or_modified_source_cannot_be_adopted(gateway, device, history):
    importer = RecordingImporter(gateway)
    source = history / FIRST
    row = importer.candidate(source)
    source.write_bytes(b"changed")
    with pytest.raises(ValueError, match="发生变化"):
        importer.adopt(device["id"], source, row)
    row = importer.candidate(source)
    gateway.backend = None
    with pytest.raises(ValueError, match="存储配置已改变"):
        importer.adopt(device["id"], source, row)
    assert gateway.query_files({})["count"] == 0


def test_rejects_missing_or_network_backend(gateway):
    gateway.backend = None
    with pytest.raises(ValueError, match="本地磁盘"):
        RecordingImporter(gateway)
    # A non-local backend must not grant filesystem browsing.
    gateway.backend = object.__new__(type("NetworkBackend", (Backend,), {
        name: lambda *args: None for name in Backend.__abstractmethods__
    }))
    with pytest.raises(ValueError, match="本地磁盘"):
        RecordingImporter(gateway)


def test_imported_files_are_sealed_against_camera_writes(gateway, device, history):
    RecordingImporter(gateway).scan(device["id"], str(history), commit=True)
    row = gateway.query_files({})["items"][0]
    with pytest.raises(OSError):
        gateway.open(device["id"], row["path"], os.O_RDWR)
    with pytest.raises(OSError):
        gateway.unlink(device["id"], row["path"])


def test_preview_expiry_uses_filename_and_includes_today(gateway, device, history, monkeypatch):
    class ImportDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2025, 10, 10, 8, tzinfo=TZ)

    monkeypatch.setattr("cammon.importer.datetime", ImportDatetime)
    gateway.update_device(device["id"], {"retention_days": 2})
    # New filesystem timestamps must not reset a historical recording's age.
    os.utime(history / FIRST, None)
    report = RecordingImporter(gateway).scan(device["id"], str(history))
    assert report["importable"] == 2 and report["expired"] == 1
    assert next(item for item in report["items"] if item.get("channel") == "00")["expired"]
    assert not next(item for item in report["items"] if item.get("channel") == "0")["expired"]
