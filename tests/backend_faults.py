"""Faults occur around real adapter operations, so retry verifies actual remote bytes."""
import errno
import os


def verify_interrupted_upload_and_retry(gateway, device, monkeypatch):
    path = "day/00_20251008090000_20251008090100.mp4"
    token = gateway.open(device["id"], path, os.O_CREAT | os.O_RDWR)
    payload = bytes(range(256)) * 512
    gateway.write(token, payload, 0)
    recording_id = gateway.handle(token).recording_id
    gateway.close(token)
    gateway.seal_candidates(gateway.recording(recording_id)["last_write"] + 1900)
    backend = gateway.backend
    upload = backend.upload

    def interrupted(remote, source):
        partial = gateway.work / "fault-partial"
        partial.write_bytes(source.read_bytes()[:128])
        try:
            upload(remote, partial)
        finally:
            partial.unlink()
        raise ConnectionError("Injected lost connection during a real partial upload")

    monkeypatch.setattr(backend, "upload", interrupted)
    assert not gateway.flush_one(recording_id)
    assert gateway.blob(recording_id).read_bytes() == payload
    assert gateway.statistics({})["count"] == 1

    def full(*_):
        raise OSError(errno.ENOSPC, "Injected remote ENOSPC")

    monkeypatch.setattr(backend, "upload", full)
    assert not gateway.flush_one(recording_id)
    assert gateway.blob(recording_id).exists()
    # Retry within this process uses the RAM index and keeps the source intact.
    monkeypatch.setattr(backend, "upload", upload)
    assert gateway.flush_one(recording_id), gateway.recording(recording_id)["error"]
    reader = gateway.open_recording(recording_id)
    try:
        assert gateway.read(reader, len(payload), 0) == payload
    finally:
        gateway.close(reader)
    assert not gateway.blob(recording_id).exists()
    assert gateway.statistics({})["count"] == 1
