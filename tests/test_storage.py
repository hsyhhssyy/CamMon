import threading

import pytest

from cammon.api import StorageConfig
from cammon.storage import CHUNK, make_backend
from tests.backend_faults import verify_interrupted_upload_and_retry


def test_local_disk_defaults():
    config = StorageConfig()
    assert config.protocol == "local" and config.address == "/data"


def test_local_disk_interruption_and_space_failure_preserve_cache(gateway, device, tmp_path, monkeypatch):
    gateway.configure_storage({"protocol": "local", "address": str(tmp_path / "mounted-disk")})
    verify_interrupted_upload_and_retry(gateway, device, monkeypatch)


def test_local_disk_upload_observes_shutdown(tmp_path):
    backend = make_backend({"protocol": "local", "address": str(tmp_path / "mounted-disk")})
    backend.stopping = threading.Event()
    backend.stopping.set()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"x" * (CHUNK + 1))
    with pytest.raises(InterruptedError):
        backend.upload("originals/video.mp4", source)
    assert backend.stat("originals/video.mp4") == 0
    assert source.stat().st_size == CHUNK + 1
    backend.stopping.clear()
    backend.upload("originals/video.mp4", source)
    with backend.reader("originals/video.mp4") as stream:
        assert stream.read() == source.read_bytes()


@pytest.mark.parametrize("operation", ["reader", "mkdirs", "upload", "stat", "rename", "remove"])
def test_local_disk_rejects_symlinks_outside_storage_root(tmp_path, operation):
    backend = make_backend({"protocol": "local", "address": str(tmp_path / "mounted-disk")})
    outside = tmp_path / "outside"
    outside.mkdir()
    source = outside / "video.mp4"
    source.write_bytes(b"preserve")
    (backend.root / "link").symlink_to(outside, target_is_directory=True)
    args = ["link/video.mp4"]
    if operation == "upload":
        args.append(source)
    elif operation == "rename":
        args.append("destination.mp4")
    with pytest.raises(ValueError, match="磁盘目录之外"):
        getattr(backend, operation)(*args)
    assert source.read_bytes() == b"preserve"
