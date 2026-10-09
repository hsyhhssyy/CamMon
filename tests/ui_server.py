"""Isolated browser-test server; never seeds or mutates a production data directory."""
import os
import tempfile
from pathlib import Path

from cammon.api import create_app
from cammon.config import Settings
from cammon.core import Gateway

directory = Path(tempfile.mkdtemp(prefix="cammon-browser-"))
settings = Settings(_env_file=None, postgres_dsn=None, secret_key=None, admin_username="admin",
                    data_dir=directory / "data", socket_path=directory / "vfs.sock",
                    samba_enabled=False, worker_enabled=False, admin_password="browser-test-password",
                    frontend_dir=Path(__file__).parents[1] / "frontend/dist", cache_reserve_bytes=0)
gateway = Gateway(settings)
device = gateway.create_device("测试摄像机", 7)
gateway.mkdir(device["id"], "day")
token = gateway.open(device["id"], "day/00_20251008080000_20251008080100.mp4", os.O_CREAT | os.O_RDWR)
gateway.write(token, b"browser-download-fixture", 0)
gateway.close(token)
app = create_app(settings, gateway)
