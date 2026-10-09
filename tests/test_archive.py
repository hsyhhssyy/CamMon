import json
import os
import shutil
import subprocess
import threading
from datetime import datetime

import pytest

from cammon.archive import Archiver, Worker
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


def test_complete_day_is_24_minutes_and_incomplete_day_is_only_recorded_time(gateway, device, videos):
    gateway.update_device(device["id"], {"retention_days": 1})
    full_id = ingest(gateway, device, "day/00_20251008000000_20251009000000.mp4", videos(86400, "1/60"))
    short_id = ingest(gateway, device, "day/01_20251008080000_20251008080100.mp4", videos(60))
    archiver = Archiver(gateway)
    assert archiver.enqueue(instant("2025-10-09T02:00:00")) == 2
    for queued in gateway.db.all("SELECT * FROM archive_jobs"):
        assert archiver.process(queued["id"]), job(gateway)["error"]
    rows = gateway.query_files({"kind": "archive"})["items"]
    assert {row["channel"] for row in rows} == {"00", "01"}
    assert next(row for row in rows if row["channel"] == "00")["duration"] == pytest.approx(1440, abs=0.2)
    assert next(row for row in rows if row["channel"] == "01")["duration"] == pytest.approx(1, abs=0.2)
    assert gateway.listdir(device["id"], "day") == []
    assert gateway.statistics({})["count"] == 2
    for recording_id in (full_id, short_id):
        assert gateway.db.one("SELECT state FROM recordings WHERE id=?", (recording_id,))["state"] == "deleted"
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
    with pytest.raises(FileNotFoundError):
        gateway.recording(source_id)
    late_id = ingest(gateway, device, "day/00_20251008080000_20251008080100.mp4", videos(60))
    assert archiver.enqueue(instant("2025-10-10T03:00:00")) == 1
    late = job(gateway, "2025-10-08")
    assert late["id"] != first["id"]
    assert archiver.process(late["id"])
    assert gateway.query_files({"kind": "archive", "day": "2025-10-08"})["count"] == 2
    with pytest.raises(FileNotFoundError):
        gateway.recording(late_id)


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
