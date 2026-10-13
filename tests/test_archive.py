import errno
import json
import os
import shutil
import subprocess
import threading
from datetime import datetime

import pytest

from cammon.archive import Archiver, Worker
from cammon.importer import RecordingImporter, copy_key
from cammon.naming import TZ, day_slices


def instant(value):
    return datetime.fromisoformat(value).replace(tzinfo=TZ).timestamp()


@pytest.fixture
def videos(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg is required for archive integration")
    def generate(seconds, rate="1"):
        path = tmp_path / f"sample-{seconds}.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=blue:s=64x48:r={rate}",
                        "-t", str(seconds), "-an", "-c:v", "libx264", "-threads", "1", str(path)], check=True)
        return path.read_bytes()
    return generate


def ingest(gateway, device, path, data):
    token = gateway.open(device["id"], path, os.O_CREAT | os.O_RDWR)
    gateway.write(token, data, 0)
    recording_id = gateway.handle(token).recording_id
    gateway.close(token)
    row = gateway.recording(recording_id)
    gateway.seal_candidates(row["last_write"] + 1900)
    assert gateway.flush_one(recording_id), gateway.recording(recording_id)["error"]
    return recording_id


def job(gateway, day=None):
    if day:
        return gateway.db.one("SELECT * FROM archive_jobs WHERE day=? ORDER BY created_at DESC", (day,))
    return gateway.db.one("SELECT * FROM archive_jobs ORDER BY created_at DESC")


def test_complete_day_is_24_minutes_and_incomplete_day_is_only_recorded_time(gateway, device, videos, tmp_path):
    root = tmp_path / "mounted-disk"
    gateway.configure_storage({"protocol": "local", "address": str(root)})
    gateway.update_device(device["id"], {"retention_days": 1})
    full_id = ingest(gateway, device, "day/00_20251008000000_20251009000000.mp4", videos(86400, "1/60"))
    short_id = ingest(gateway, device, "day/01_20251008080000_20251008080100.mp4", videos(60))
    originals = [gateway.recording(recording_id) for recording_id in (full_id, short_id)]
    attributes = {row["id"]: gateway.stat(device["id"], row["path"]) for row in originals}
    archiver = Archiver(gateway)
    assert archiver.enqueue(instant("2025-10-09T02:00:00")) == 2
    for queued in gateway.db.all("SELECT * FROM archive_jobs"):
        assert archiver.process(queued["id"]), job(gateway)["error"]
    rows = gateway.query_files({"kind": "archive"})["items"]
    assert all((root / row["remote_path"]).stat().st_size == row["size"] for row in rows)
    assert {row["channel"] for row in rows} == {"00", "01"}
    assert next(row for row in rows if row["channel"] == "00")["duration"] == pytest.approx(1440, abs=0.2)
    assert next(row for row in rows if row["channel"] == "01")["duration"] == pytest.approx(1, abs=0.2)
    assert gateway.listdir(device["id"], "day") == sorted(row["path"].split("/")[-1] for row in originals)
    assert gateway.statistics({})["count"] == 2
    assert gateway.statistics({})["bytes"] == sum(row["size"] for row in rows)
    assert gateway.query_files({"kind": "original"})["count"] == 0
    for recording_id in (full_id, short_id):
        virtual = gateway.recording(recording_id)
        assert virtual["state"] == "virtual"
        assert virtual["duration"] == pytest.approx(virtual["end"] - virtual["start"], abs=0.2)
        assert gateway.stat(device["id"], virtual["path"]) == attributes[recording_id]
    assert not list((root / "originals").rglob("*.mp4"))
    assert archiver.enqueue(instant("2030-01-01T02:00:00")) == 0


def test_midnight_coverage_read_lease_and_late_append_archive(gateway, device, videos):
    gateway.update_device(device["id"], {"retention_days": 1})
    source_id = ingest(gateway, device, "day/00_20251008235900_20251009000100.mp4", videos(120))
    archiver = Archiver(gateway)
    assert archiver.enqueue(instant("2025-10-09T02:00:00")) == 1
    first = job(gateway, "2025-10-08")
    assert json.loads(first["sources"])[0]["offset"] == 0
    assert archiver.process(first["id"])
    assert gateway.recording(source_id)["state"] == "stored"
    assert archiver.enqueue(instant("2025-10-10T02:00:00")) == 1
    second = job(gateway, "2025-10-09")
    assert json.loads(second["sources"])[0]["offset"] == 60
    lease = gateway.open_recording(source_id)
    assert archiver.process(second["id"])
    assert gateway.read(lease, 10, 0)
    gateway.close(lease)
    archiver.collect_originals()
    assert gateway.recording(source_id)["state"] == "virtual"
    late_id = ingest(gateway, device, "day/00_20251008080000_20251008080100.mp4", videos(60))
    assert archiver.enqueue(instant("2025-10-10T03:00:00")) == 1
    late = job(gateway, "2025-10-08")
    assert late["id"] != first["id"]
    assert archiver.process(late["id"])
    assert gateway.query_files({"kind": "archive", "day": "2025-10-08"})["count"] == 2
    assert gateway.recording(late_id)["state"] == "virtual"


def test_virtual_original_supports_metadata_handles_and_blocks_reuploads(gateway, device, videos, monkeypatch):
    source = "day/00_20251008080000_20251008080100.mp4"
    source_id = ingest(gateway, device, source, videos(60))
    before = gateway.stat(device["id"], source)
    archiver = Archiver(gateway)
    archiver.enqueue(instant("2025-12-01T02:00:00"))
    assert archiver.process(job(gateway)["id"])
    assert gateway.stat(device["id"], source) == before
    assert not gateway.blob(source_id).exists()
    assert not gateway.backend.exists(gateway.recording(source_id)["remote_path"])
    assert gateway._projection(device, source).stat().st_size == 0
    assert gateway.open(device["id"], source, os.O_PATH) == 0
    with pytest.raises(FileExistsError):
        gateway.open(device["id"], source, os.O_CREAT | os.O_EXCL | os.O_RDWR)
    for flags in (os.O_RDWR, os.O_CREAT | os.O_RDWR, os.O_TRUNC | os.O_RDWR, os.O_TRUNC | os.O_RDONLY):
        with pytest.raises(OSError) as failure:
            gateway.open(device["id"], source, flags)
        assert failure.value.errno == errno.EACCES
    for operation in (
        lambda: gateway.unlink(device["id"], source),
        lambda: gateway.rename(device["id"], source, source.replace("00_", "01_")),
        lambda: gateway.rename(device["id"], "day", "renamed"),
        lambda: gateway.unlink(device["id"], "day", directory=True),
    ):
        with pytest.raises(OSError):
            operation()
    assert gateway.query_files({"kind": "original"})["count"] == 0
    assert archiver.enqueue(instant("2030-01-01T02:00:00")) == 0

    def no_backend_read(*args):
        pytest.fail("A virtual recording must never read removed backend video data")
    monkeypatch.setattr(gateway.backend, "reader", no_backend_read)
    token = gateway.open(device["id"], source, os.O_RDONLY)
    assert gateway.stat(device["id"], gateway.recording(gateway.handle(token).recording_id)["path"]) == before
    gateway.sync(token)
    with pytest.raises(OSError) as failure:
        gateway.read(token, 16, 0)
    assert failure.value.errno == errno.ENODATA
    gateway.close(token)
    assert not gateway.handles


def test_upgrade_restores_only_fully_archived_paths_and_keeps_existing_files(gateway, device, videos):
    gateway.mkdir(device["id"], "day/hour")
    source = "day/hour/00_20251008080000_20251008080100.mp4"
    source_id = ingest(gateway, device, source, videos(60))
    cross = "day/01_20251008235900_20251009000100.mp4"
    cross_id = ingest(gateway, device, cross, videos(120))
    gateway.update_device(device["id"], {"retention_days": 1})
    archiver = Archiver(gateway)
    archiver.enqueue(instant("2025-10-09T02:00:00"))
    for queued in gateway.db.all("SELECT * FROM archive_jobs"):
        assert archiver.process(queued["id"])
    gateway.db.execute("UPDATE recordings SET state='deleted',duration=NULL WHERE id IN (?,?)", (source_id, cross_id))
    gateway._projection(device, source).unlink()
    cached = "day/02_20251008080000_20251008080100.mp4"
    token = gateway.open(device["id"], cached, os.O_CREAT | os.O_RDWR)
    cached_id = gateway.handle(token).recording_id
    gateway.close(token)
    gateway.unlink(device["id"], cached)
    shutil.rmtree(gateway._projection(device, "day"))
    gateway.db.execute("DELETE FROM directories WHERE device_id=?", (device["id"],))
    gateway.recover()
    assert gateway.recording(source_id)["state"] == "virtual"
    assert gateway.recording(source_id)["duration"] == 60
    assert gateway._projection(device, source).exists()
    assert gateway.db.one("SELECT state FROM recordings WHERE id=?", (cross_id,))["state"] == "deleted"
    assert gateway.db.one("SELECT state FROM recordings WHERE id=?", (cached_id,))["state"] == "deleted"
    assert gateway.listdir(device["id"], "day") == ["hour"]
    assert gateway.listdir(device["id"], "day/hour") == [source.split("/")[-1]]

    # An old tombstone must not hide a same-path recording uploaded before upgrade.
    gateway.db.execute("UPDATE recordings SET state='deleted' WHERE id=?", (source_id,))
    replacement_id = ingest(gateway, device, source, videos(60))
    gateway.recover()
    assert gateway.by_path(device["id"], source)["id"] == replacement_id
    assert gateway.recording(replacement_id)["state"] == "stored"
    assert gateway.db.one("SELECT state FROM recordings WHERE id=?", (source_id,))["state"] == "deleted"


def test_retention_is_per_device_and_uses_beijing_inclusive_days(gateway, device):
    gateway.update_device(device["id"], {"retention_days": 1})
    protected = gateway.create_device("长保留设备", retention_days=30)
    gateway.mkdir(protected["id"], "day")
    ingest(gateway, device, "day/00_20251008080000_20251008080100.mp4", b"bad-video")
    ingest(gateway, protected, "day/00_20251008080000_20251008080100.mp4", b"bad-video")
    archiver = Archiver(gateway)
    # UTC 16:00 is already the next Beijing natural day.
    assert archiver.enqueue(datetime.fromisoformat("2025-10-08T15:59:59+00:00").timestamp()) == 0
    assert archiver.enqueue(datetime.fromisoformat("2025-10-08T16:00:00+00:00").timestamp()) == 1
    assert job(gateway)["device_id"] == device["id"]
    assert archiver.enqueue(instant("2025-10-09T02:00:00")) == 0


def test_corrupt_or_mistimed_video_preserves_original_and_reports_failure(gateway, device, videos):
    gateway.update_device(device["id"], {"retention_days": 1})
    source_id = ingest(gateway, device, "day/00_20251008080000_20251008080100.mp4", b"corrupt")
    archiver = Archiver(gateway)
    archiver.enqueue(instant("2025-10-09T02:00:00"))
    assert not archiver.process(job(gateway)["id"])
    assert job(gateway)["status"] == "failed" and job(gateway)["error"]
    assert gateway.recording(source_id)["state"] == "stored"
    assert gateway.backend.exists(gateway.recording(source_id)["remote_path"])
    assert gateway.db.all("SELECT * FROM coverage") == []
    source_path = gateway.backend._path(gateway.recording(source_id)["remote_path"])
    source_path.write_bytes(videos(5))
    assert not archiver.process(job(gateway)["id"])
    assert "时长" in job(gateway)["error"]
    assert gateway.statistics({})["count"] == 1


def test_publication_ack_loss_can_recover_without_overwriting_archive(gateway, device, videos, monkeypatch):
    gateway.update_device(device["id"], {"retention_days": 1})
    source_id = ingest(gateway, device, "day/00_20251008080000_20251008080100.mp4", videos(60))
    archiver = Archiver(gateway)
    archiver.enqueue(instant("2025-10-09T02:00:00"))
    current = job(gateway)
    real_publish = gateway.publish
    def lose_ack(*args):
        real_publish(*args)
        raise ConnectionError("published but acknowledgement lost")
    monkeypatch.setattr(gateway, "publish", lose_ack)
    assert not archiver.process(current["id"])
    assert gateway.recording(source_id)["state"] == "stored"
    assert gateway.statistics({})["count"] == 1
    monkeypatch.setattr(gateway, "publish", real_publish)
    monkeypatch.setattr(archiver, "_ffmpeg", lambda *_: pytest.fail("A verified published archive must not be transcoded again"))
    assert archiver.process(current["id"])
    assert gateway.statistics({})["count"] == 1
    assert gateway.query_files({"kind": "archive"})["count"] == 1
    assert not archiver.process(current["id"])


def test_committed_archive_cannot_be_invalidated_by_cleanup_failure(gateway, device, videos, monkeypatch):
    gateway.update_device(device["id"], {"retention_days": 1})
    source_id = ingest(gateway, device, "day/00_20251008080000_20251008080100.mp4", videos(60))
    archiver = Archiver(gateway)
    archiver.enqueue(instant("2025-10-09T02:00:00"))
    current = job(gateway)
    collect = archiver.collect_originals
    def fail_cleanup():
        raise OSError("Injected cleanup error after successful publication")
    monkeypatch.setattr(archiver, "collect_originals", fail_cleanup)
    assert archiver.process(current["id"])
    assert job(gateway)["status"] == "done"
    assert gateway.query_files({"kind": "archive"})["count"] == 1
    assert gateway.recording(source_id)["state"] == "stored"
    monkeypatch.setattr(archiver, "collect_originals", collect)
    collect()
    assert gateway.statistics({})["count"] == 1


def test_deletion_network_wait_does_not_block_camera_writes(gateway, device, monkeypatch):
    source_id = ingest(gateway, device, "day/00_20251008080000_20251008080100.mp4", b"source")
    gateway.db.execute("INSERT INTO archive_jobs(id,device_id,channel,day,sources,status,created_at,updated_at) "
                       "VALUES ('covered',?,'00','2025-10-08','[]','done',0,0)", (device["id"],))
    gateway.db.execute("INSERT INTO coverage VALUES (?, '2025-10-08', 'covered')", (source_id,))
    entered, release = threading.Event(), threading.Event()
    real_remove = gateway.backend.remove
    def delayed_remove(path):
        entered.set()
        assert release.wait(5)
        real_remove(path)
    monkeypatch.setattr(gateway.backend, "remove", delayed_remove)
    collector = threading.Thread(target=Archiver(gateway).collect_originals)
    collector.start()
    assert entered.wait(2)
    try:
        token = gateway.open(device["id"], "day/01_20251008080000_20251008080100.mp4", os.O_CREAT | os.O_RDWR)
        gateway.write(token, b"camera continues", 0)
        gateway.close(token)
        with pytest.raises(FileNotFoundError):
            gateway.open_recording(source_id)
    finally:
        release.set()
        collector.join(5)
    assert not collector.is_alive()


def test_stop_cancels_transcoding_without_losing_sources(gateway, device, monkeypatch):
    worker = Worker(gateway)
    command = ["sleep", "30"]
    errors = []
    def run():
        try:
            worker.archiver._run_process(command, timeout=60)
        except InterruptedError as exc:
            errors.append(exc)
    worker.thread = threading.Thread(target=run)
    worker.start()
    worker.stop()
    assert not worker.thread.is_alive()
    assert errors


def test_midnight_endpoint_belongs_only_to_previous_day():
    assert day_slices(instant("2025-10-08T23:59:00"), instant("2025-10-09T00:00:00")) == [
        ("2025-10-08", 0, 60),
    ]


def test_imported_video_waits_for_next_daily_tick_then_generates_archive(gateway, device, videos, monkeypatch):
    gateway.update_device(device["id"], {"retention_days": 1})
    folder = gateway.backend.root / "history"
    folder.mkdir()
    source = folder / "00_20251008080000_20251008080100.mp4"
    source.write_bytes(videos(60))
    now = datetime.fromisoformat("2025-10-09T03:00:00").replace(tzinfo=TZ)

    class ScheduledDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr("cammon.archive.datetime", ScheduledDatetime)
    gateway.set_setting("last_archive_enqueue", "2025-10-09")
    report = RecordingImporter(gateway).scan(device["id"], str(folder), commit=True)
    assert report["imported"] == 1 and report["expired"] == 1
    worker = Worker(gateway)
    worker.tick()
    assert source.exists() and gateway.query_files({"kind": "archive"})["count"] == 0
    now = datetime.fromisoformat("2025-10-10T02:00:00").replace(tzinfo=TZ)
    worker.tick()
    archive = gateway.query_files({"kind": "archive"})["items"][0]
    assert archive["duration"] == pytest.approx(1, abs=0.2)
    assert worker.archiver.probe(gateway.backend._path(archive["remote_path"]))["codec_name"] == "h264"
    assert not source.exists() and gateway.query_files({"kind": "original"})["count"] == 0


def test_imported_cross_midnight_video_preserves_source_until_both_days_archived(gateway, device, videos):
    gateway.update_device(device["id"], {"retention_days": 1})
    folder = gateway.backend.root / "history"
    folder.mkdir()
    source = folder / "00_20251008235900_20251009000100.mp4"
    source.write_bytes(videos(120))
    RecordingImporter(gateway).scan(device["id"], str(folder), commit=True)
    archiver = Archiver(gateway)
    assert archiver.enqueue(instant("2025-10-09T02:00:00")) == 1
    assert archiver.process(job(gateway)["id"])
    assert source.exists()
    assert archiver.enqueue(instant("2025-10-10T02:00:00")) == 1
    assert archiver.process(job(gateway)["id"])
    assert not source.exists()
    assert gateway.query_files({"kind": "archive"})["count"] == 2


def test_corrupt_imported_video_is_retained_on_archive_failure(gateway, device):
    gateway.update_device(device["id"], {"retention_days": 1})
    folder = gateway.backend.root / "history"
    folder.mkdir()
    source = folder / "00_20251008080000_20251008080100.mp4"
    source.write_bytes(b"corrupt historical video")
    RecordingImporter(gateway).scan(device["id"], str(folder), commit=True)
    archiver = Archiver(gateway)
    assert archiver.enqueue(instant("2025-10-09T02:00:00")) == 1
    assert not archiver.process(job(gateway)["id"])
    assert source.read_bytes() == b"corrupt historical video"
    assert gateway.query_files({"kind": "original"})["count"] == 1


def test_existing_camera_recording_and_import_copy_archive_once_and_delete_both(gateway, device, videos):
    gateway.update_device(device["id"], {"retention_days": 1})
    name = "00_20251008080000_20251008080100.mp4"
    data = videos(60)
    original_id = ingest(gateway, device, "day/" + name, data)
    original_path = gateway.backend._path(gateway.recording(original_id)["remote_path"])
    folder = gateway.backend.root / "history"
    folder.mkdir()
    source = folder / name
    source.write_bytes(data)
    report = RecordingImporter(gateway).scan(device["id"], str(folder), commit=True)
    assert report["copies"] == 1 and report["imported"] == 0
    assert gateway.query_files({})["count"] == 1
    assert source.exists() and original_path.exists()
    archiver = Archiver(gateway)
    archiver.collect_originals()
    assert source.exists() and original_path.exists()  # Not deleted by importing.
    assert archiver.enqueue(instant("2025-10-09T02:00:00")) == 1
    current = job(gateway)
    assert len(json.loads(current["sources"])) == 1
    assert archiver.process(current["id"])
    archive = gateway.query_files({"kind": "archive"})["items"][0]
    assert archive["duration"] == pytest.approx(1, abs=0.2)  # No duplicate playback.
    assert not source.exists() and not original_path.exists()
    assert gateway.get_setting(copy_key("history/" + name)) is None


def test_failed_archive_keeps_camera_original_and_linked_import_copy(gateway, device):
    gateway.update_device(device["id"], {"retention_days": 1})
    name = "00_20251008080000_20251008080100.mp4"
    original_id = ingest(gateway, device, "day/" + name, b"corrupt video")
    folder = gateway.backend.root / "history"
    folder.mkdir()
    source = folder / name
    source.write_bytes(b"corrupt video")
    assert RecordingImporter(gateway).scan(device["id"], str(folder), commit=True)["copies"] == 1
    archiver = Archiver(gateway)
    archiver.enqueue(instant("2025-10-09T02:00:00"))
    assert not archiver.process(job(gateway)["id"])
    archiver.collect_originals()
    assert source.exists() and gateway.backend.exists(gateway.recording(original_id)["remote_path"])


def test_modified_duplicate_is_preserved_when_camera_original_is_archived(gateway, device, videos):
    gateway.update_device(device["id"], {"retention_days": 1})
    name = "00_20251008080000_20251008080100.mp4"
    data = videos(60)
    ingest(gateway, device, "day/" + name, data)
    folder = gateway.backend.root / "history"
    folder.mkdir()
    source = folder / name
    source.write_bytes(data)
    assert RecordingImporter(gateway).scan(device["id"], str(folder), commit=True)["copies"] == 1
    modified = b"x" * len(data)  # Same size; cleanup must check the content again.
    source.write_bytes(modified)
    archiver = Archiver(gateway)
    archiver.enqueue(instant("2025-10-09T02:00:00"))
    assert archiver.process(job(gateway)["id"])
    assert source.read_bytes() == modified
    saved = gateway.get_setting(copy_key("history/" + name))
    assert "内容已改变" in saved["error"]


def test_copy_of_already_archived_camera_video_is_cleaned_without_rearchiving(gateway, device, videos):
    gateway.update_device(device["id"], {"retention_days": 1})
    name = "00_20251008080000_20251008080100.mp4"
    data = videos(60)
    original_id = ingest(gateway, device, "day/" + name, data)
    archiver = Archiver(gateway)
    archiver.enqueue(instant("2025-10-09T02:00:00"))
    assert archiver.process(job(gateway)["id"])
    assert gateway.recording(original_id)["state"] == "virtual"
    folder = gateway.backend.root / "history"
    folder.mkdir()
    source = folder / name
    source.write_bytes(data)
    assert RecordingImporter(gateway).scan(device["id"], str(folder), commit=True)["copies"] == 1
    assert source.exists()  # Linking does not delete it in the HTTP request.
    archiver.collect_originals()
    assert not source.exists()
    assert gateway.query_files({"kind": "archive"})["count"] == 1
    assert archiver.enqueue(instant("2025-10-10T02:00:00")) == 0


def test_duplicate_cross_midnight_copy_stays_until_all_days_are_archived(gateway, device, videos):
    gateway.update_device(device["id"], {"retention_days": 1})
    name = "00_20251008235900_20251009000100.mp4"
    data = videos(120)
    ingest(gateway, device, "day/" + name, data)
    folder = gateway.backend.root / "history"
    folder.mkdir()
    source = folder / name
    source.write_bytes(data)
    RecordingImporter(gateway).scan(device["id"], str(folder), commit=True)
    archiver = Archiver(gateway)
    archiver.enqueue(instant("2025-10-09T02:00:00"))
    assert archiver.process(job(gateway)["id"])
    assert source.exists()
    archiver.enqueue(instant("2025-10-10T02:00:00"))
    assert archiver.process(job(gateway)["id"])
    assert not source.exists() and gateway.query_files({"kind": "archive"})["count"] == 2
