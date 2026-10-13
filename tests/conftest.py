import shutil
import subprocess
from pathlib import Path

import pytest

from cammon.config import Settings
from cammon.core import Gateway
from cammon.storage import LocalBackend


@pytest.fixture
def videos(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg and ffprobe are required for video tests")

    def generate(seconds, rate="1"):
        path = tmp_path / f"sample-{seconds}.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=blue:s=64x48:r={rate}",
                        "-t", str(seconds), "-an", "-c:v", "libx264", "-threads", "1", str(path)], check=True)
        return path.read_bytes()

    return generate


@pytest.fixture
def gateway(tmp_path):
    settings = Settings(_env_file=None, postgres_dsn=None, secret_key=None, admin_username="admin",
                        data_dir=tmp_path / "data", socket_path=tmp_path / "vfs.sock",
                        admin_password="test-admin-password", samba_enabled=False, worker_enabled=False,
                        cache_reserve_bytes=0, frontend_dir=Path(__file__).parents[1] / "frontend/dist")
    gateway = Gateway(settings)
    gateway.backend = LocalBackend(tmp_path / "remote")
    yield gateway
    gateway.shutdown()


@pytest.fixture
def device(gateway):
    device = gateway.create_device("门口摄像机")
    gateway.mkdir(device["id"], "day")
    return device
