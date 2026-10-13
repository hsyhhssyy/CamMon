"""Isolated browser-test server; never seeds or mutates a production data directory."""
import os
import subprocess
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
video = directory / "fixture.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25",
                "-t", "60", "-c:v", "libx264", "-threads", "1", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", "-g", "25", "-movflags", "+faststart", str(video)], check=True)
token = gateway.open(device["id"], "day/00_20251008080000_20251008080100.mp4", os.O_CREAT | os.O_RDWR)
gateway.write(token, video.read_bytes(), 0)
gateway.close(token)
for channel, data in [("98", b"invalid-video"), ("99", b"")]:
    token = gateway.open(device["id"], f"day/{channel}_20251008080000_20251008080100.mp4", os.O_CREAT | os.O_RDWR)
    if data:
        gateway.write(token, data, 0)
    gateway.close(token)
archive = directory / "archive.mp4"
subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-vf", "setpts=(PTS-STARTPTS)/60",
                "-r", "25", "-c:v", "libx264", "-threads", "1", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(archive)], check=True)
row = gateway.recording(1)
archive_id = gateway.db.execute(
    "INSERT INTO recordings(device_id,path,channel,start,end,day,kind,state,size,created_at,last_write) "
    "VALUES (?,?,?,?,?,?,'archive','stored',?,?,?)",
    (device["id"], "00_2025-10-08_archive.mp4", "00", row["start"], row["end"], row["day"],
     archive.stat().st_size, row["created_at"], row["last_write"]),
).lastrowid
gateway.blob(archive_id).write_bytes(archive.read_bytes())
app = create_app(settings, gateway)
