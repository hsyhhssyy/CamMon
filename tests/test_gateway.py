import errno
import os
import threading
import time

import pytest

from cammon.core import Gateway

FIRST = "day/00_20251008080000_20251008080100.mp4"
SECOND = "day/00_20251008080100_20251008080200.mp4"
OTHER = "day/01_20251008080100_20251008080200.mp4"


def create(gateway, device, path=FIRST, data=b"video-data"):
    token = gateway.open(device["id"], path, os.O_CREAT | os.O_RDWR)
    gateway.write(token, data, 0)
    return token


@pytest.mark.parametrize("path", [
    "00_20251008080000_20251008080100.mp4", "day/file.txt", "day/0_20250230000000_20250301000000.mp4",
    "day/0_20250101000000_20241231000000.mp4", "day/0_2025100808000_20251008080100.mp4",
    "day/00_20251008080000_20251008080100.mp4:stream", "../escape.mp4", "/absolute.mp4",
    "day/../00_20251008080000_20251008080100.mp4", "day/a/b/00_20251008080000_20251008080100.mp4",
])
def test_invalid_names_never_create_files(gateway, device, path):
    with pytest.raises(OSError):
        create(gateway, device, path)
    assert gateway.query_files({})["count"] == 0
    assert not list(gateway.blobs.iterdir())


def test_directory_depth_and_header_writes(gateway, device):
    gateway.mkdir(device["id"], "day/hour")
    with pytest.raises(OSError):
        gateway.mkdir(device["id"], "day/hour/minute")
    token = create(gateway, device, FIRST.replace("day/", "day/hour/"), b"0000DATA")
    gateway.write(token, b"MOOV", 0)
    gateway.write(token, b"TAIL", 8)
    assert gateway.read(token, 12, 0) == b"MOOVDATATAIL"
    assert gateway.statistics({"channel": "00"})["bytes"] == 12
    gateway.close(token)


def test_directory_rename_revalidates_depth_and_preserves_file_identity(gateway, device):
    gateway.mkdir(device["id"], "day/hour")
    token = create(gateway, device, FIRST.replace("day/", "day/hour/"))
    inode = gateway.stat(device["id"], "day/hour")["inode"]
    gateway.rename(device["id"], "day", "renamed")
    assert gateway.stat(device["id"], "renamed/hour")["inode"] == inode
    assert gateway.recording(1)["path"].startswith("renamed/hour/")
    assert gateway.read(token, 10, 0) == b"video-data"
    gateway.mkdir(device["id"], "parent")
    with pytest.raises(OSError):
        gateway.rename(device["id"], "renamed", "parent/too-deep")
    gateway.close(token)
    gateway.seal_candidates(time.time() + 1900)
    with pytest.raises(OSError):
        gateway.rename(device["id"], "renamed", "other")


def test_camera_may_delete_closed_cached_file_but_not_sealed_file(gateway, device):
    token = create(gateway, device)
    with pytest.raises(OSError):
        gateway.unlink(device["id"], FIRST)
    gateway.close(token)
    gateway.unlink(device["id"], FIRST)
    assert gateway.listdir(device["id"], "day") == []
    assert gateway.statistics({})["count"] == 0
    assert not gateway.blob(1).exists()
    token = create(gateway, device)
    gateway.close(token)
    gateway.seal_candidates(time.time() + 1900)
    with pytest.raises(OSError):
        gateway.unlink(device["id"], FIRST)


def test_successors_are_per_channel_and_wait_for_all_writers(gateway, device):
    first = create(gateway, device)
    other = create(gateway, device, OTHER)
    gateway.close(other)
    now = time.time()
    assert gateway.seal_candidates(now + 20) == []
    second = create(gateway, device, SECOND)
    gateway.write(first, b"HEADER", 0)
    assert gateway.seal_candidates(now + 20) == []
    gateway.close(first)
    assert gateway.seal_candidates(now + 20) == [1]
    assert gateway.recording(2)["state"] == "cached"
    assert gateway.recording(3)["state"] == "cached"
    gateway.close(second)


def test_new_file_in_other_directory_seals_same_channel(gateway, device):
    gateway.mkdir(device["id"], "other")
    token = create(gateway, device)
    gateway.close(token)
    next_token = create(gateway, device, SECOND.replace("day/", "other/"))
    assert gateway.seal_candidates(time.time() + 20) == [1]
    gateway.close(next_token)


def test_late_older_segment_does_not_seal_the_current_segment(gateway, device):
    current = create(gateway, device, SECOND)
    gateway.close(current)
    earlier = create(gateway, device, FIRST)
    gateway.close(earlier)
    assert gateway.seal_candidates(time.time() + 20) == [2]
    assert gateway.recording(1)["state"] == "cached"


def test_valid_channel_rename_updates_metadata_and_successor_without_changing_identity(gateway, device):
    first = create(gateway, device, FIRST)
    other = create(gateway, device, OTHER)
    renamed = FIRST.replace("00_", "01_")
    inode = gateway.stat(device["id"], FIRST)["inode"]
    gateway.rename(device["id"], FIRST, renamed)
    assert gateway.recording(1)["channel"] == "01"
    assert gateway.stat(device["id"], renamed)["inode"] == inode
    assert gateway.read(first, 10, 0) == b"video-data"
    gateway.close(first)
    assert gateway.seal_candidates(time.time() + 20) == [1]
    assert gateway.recording(2)["state"] == "cached"
    gateway.close(other)


def test_cache_space_failure_is_visible_and_clears_after_space_is_released(gateway, device):
    gateway.settings.cache_limit_bytes = 10
    token = create(gateway, device)
    with pytest.raises(OSError):
        gateway.write(token, b"more", 10)
    assert gateway.status()["cache_error"]
    gateway.truncate(token, 0)
    assert gateway.status()["cache_error"] is None
    gateway.close(token)


def test_remote_reader_close_does_not_hold_camera_lock(gateway, device, monkeypatch):
    token = create(gateway, device)
    gateway.close(token)
    gateway.seal_candidates(time.time() + 1900)
    assert gateway.flush_one(1)
    entered, release = threading.Event(), threading.Event()
    real_reader = gateway.backend.reader
    class SlowClose:
        def __init__(self, path):
            self.stream = real_reader(path)
        def read(self, count):
            return self.stream.read(count)
        def seek(self, offset):
            return self.stream.seek(offset)
        def close(self):
            entered.set()
            assert release.wait(5)
            self.stream.close()
    monkeypatch.setattr(gateway.backend, "reader", SlowClose)
    reader = gateway.open_recording(1)
    assert gateway.read(reader, 4, 0) == b"vide"
    closer = threading.Thread(target=gateway.close, args=(reader,))
    closer.start()
    assert entered.wait(2)
    try:
        new_token = create(gateway, device, OTHER)
        gateway.close(new_token)
    finally:
        release.set()
        closer.join(5)
    assert not closer.is_alive()


def test_idle_tail_remains_writable_until_30_minutes(gateway, device):
    token = create(gateway, device)
    gateway.close(token)
    modified = gateway.recording(1)["last_write"]
    assert gateway.seal_candidates(modified + 1799) == []
    reopened = gateway.open(device["id"], FIRST, os.O_RDWR)
    assert gateway.seal_candidates(modified + 2000) == []
    gateway.close(reopened)
    assert gateway.seal_candidates(modified + 1801) == [1]


def test_flush_keeps_identity_listing_and_reader_handles(gateway, device):
    writer = create(gateway, device)
    reader = gateway.open(device["id"], FIRST, os.O_RDONLY)
    inode = gateway.stat(device["id"], FIRST)["inode"]
    gateway.close(writer)
    gateway.seal_candidates(time.time() + 1900)
    assert gateway.flush_one(1)
    assert not gateway.blob(1).exists()
    assert gateway.listdir(device["id"], "day") == [FIRST.split("/")[-1]]
    assert gateway.stat(device["id"], FIRST)["inode"] == inode
    assert gateway.stat(device["id"], FIRST)["size"] == 10
    assert gateway.read(reader, 10, 0) == b"video-data"
    gateway.close(reader)
    stored_reader = gateway.open(device["id"], FIRST, os.O_RDONLY)
    assert gateway.read(stored_reader, 4, 6) == b"data"
    gateway.close(stored_reader)
    for action in [lambda: gateway.open(device["id"], FIRST, os.O_RDWR),
                   lambda: gateway.rename(device["id"], FIRST, SECOND),
                   lambda: gateway.unlink(device["id"], FIRST)]:
        with pytest.raises(OSError):
            action()
    assert gateway.statistics({}) == {"count": 1, "bytes": 10, "groups": [
        {"day": "2025-10-08", "kind": "original", "count": 1, "bytes": 10}]}


def test_upload_interruption_preserves_cache_and_reconciles_publication(gateway, device, monkeypatch):
    token = create(gateway, device)
    gateway.close(token)
    gateway.seal_candidates(time.time() + 1900)
    rename = gateway.backend.rename

    def interrupt(source, destination):
        rename(source, destination)
        raise ConnectionError("Lost acknowledgement after rename")

    monkeypatch.setattr(gateway.backend, "rename", interrupt)
    assert not gateway.flush_one(1)
    assert gateway.blob(1).read_bytes() == b"video-data"
    assert gateway.recording(1)["state"] == "pending"
    monkeypatch.setattr(gateway.backend, "rename", rename)

    def unexpected_upload(*args):
        pytest.fail("An already published, matching file should not be re-uploaded")

    monkeypatch.setattr(gateway.backend, "upload", unexpected_upload)
    assert gateway.flush_one(1)
    assert gateway.query_files({})["count"] == 1


def test_partial_upload_and_verification_failure_never_discard_source(gateway, device, monkeypatch):
    token = create(gateway, device)
    gateway.close(token)
    gateway.seal_candidates(time.time() + 1900)
    upload = gateway.backend.upload

    def corrupt(path, source):
        upload(path, source)
        gateway.backend._path(path).write_bytes(b"wrong")

    monkeypatch.setattr(gateway.backend, "upload", corrupt)
    assert not gateway.flush_one(1)
    assert gateway.blob(1).read_bytes() == b"video-data"
    assert "verification" in gateway.recording(1)["error"]
    assert not gateway.backend.exists(gateway.recording(1)["remote_path"])
    monkeypatch.setattr(gateway.backend, "upload", upload)
    assert gateway.flush_one(1)


def test_cache_limit_rejects_extension_but_permits_header_updates(gateway, device):
    gateway.settings.cache_limit_bytes = 10
    token = create(gateway, device)
    with pytest.raises(OSError) as error:
        gateway.write(token, b"more", 10)
    assert error.value.errno == errno.ENOSPC
    gateway.write(token, b"HEAD", 0)
    assert gateway.read(token, 10, 0) == b"HEADo-data"
    gateway.close(token)


def test_rename_updates_time_metadata_without_changing_identity(gateway, device):
    token = create(gateway, device)
    renamed = "day/00_20251008080000_20251008080300.mp4"
    inode = gateway.stat(device["id"], FIRST)["inode"]
    gateway.rename(device["id"], FIRST, renamed)
    assert gateway.read(token, 10, 0) == b"video-data"
    assert gateway.stat(device["id"], renamed)["inode"] == inode
    assert gateway.recording(1)["end"] - gateway.recording(1)["start"] == 180
    with pytest.raises(OSError):
        gateway.rename(device["id"], renamed, renamed.replace("20251008080300", "20251008075959"))
    gateway.close(token)


def test_disabled_device_closes_sessions_and_rejects_access(gateway, device):
    token = create(gateway, device)
    gateway.update_device(device["id"], {"enabled": False})
    assert token not in gateway.handles
    with pytest.raises(OSError):
        gateway.listdir(device["id"])
    assert gateway.query_files({})["count"] == 1


def test_restart_discards_memory_metadata_and_temporary_recordings(tmp_path):
    from cammon.config import Settings

    settings = Settings(_env_file=None, postgres_dsn=None, secret_key=None,
                        data_dir=tmp_path / "data", admin_password="test-admin-password", samba_enabled=False)
    first = Gateway(settings)
    device = first.create_device("camera")
    first.mkdir(device["id"], "day")
    token = create(first, device)
    first.close(token)
    first.db.execute("UPDATE recordings SET state='uploading',size=0")
    first.shutdown()
    second = Gateway(settings)
    try:
        assert not second.devices()
        assert second.query_files({})["count"] == 0
        assert not list(second.blobs.iterdir())
        assert not (settings.data_dir / "index.sqlite3").exists()
        assert not (settings.data_dir / "secret.key").exists()
    finally:
        second.shutdown()
