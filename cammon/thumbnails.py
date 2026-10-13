"""Small, persistent first-frame previews; source video bytes stay in place."""

import hashlib
import json
import stat
import tempfile
import threading
from pathlib import Path

from cammon.media import MediaTools
from cammon.storage import LocalBackend


class Thumbnails:
    def __init__(self, gateway):
        self.gateway = gateway
        # Bound decoding work across previews, ingestion and archive jobs.
        self.lock = threading.Lock()

    @staticmethod
    def source_key(source: Path) -> str:
        info = source.lstat()
        if not stat.S_ISREG(info.st_mode) or not info.st_size:
            raise ValueError("首帧预览需要非空的普通视频文件")
        identity = ["first-frame-v1", str(source), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]
        digest = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        return f"thumbnails/{digest[:2]}/{digest}.jpg"

    def prepare(self, source: Path, backend, expected_key: str | None = None, *, destination: str | None = None) -> str:
        with self.lock:
            backend.check_cancelled()
            signature = self.source_key(source)
            key = destination or signature
            if expected_key and signature != expected_key:
                raise ValueError("视频在预览后发生变化，请重新预览")
            if backend.exists(key):
                return key
            with tempfile.TemporaryDirectory(prefix="thumbnail-", dir=self.gateway.work) as directory:
                output = Path(directory) / "first-frame.jpg"
                MediaTools(self.gateway)._run_process([
                    self.gateway.settings.ffmpeg, "-nostdin", "-v", "error", "-y",
                    "-threads", str(self.gateway.settings.ffmpeg_threads), "-i", str(source),
                    "-map", "0:v:0", "-frames:v", "1", "-an",
                    "-vf", "scale=480:270:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1",
                    "-c:v", "mjpeg", "-threads", "1", "-q:v", "3", str(output),
                ], timeout=30)
                if not output.is_file() or not output.stat().st_size:
                    raise ValueError("视频没有可解码的首帧")
                if self.source_key(source) != signature:
                    raise ValueError("视频在提取首帧时发生变化，请停止写入后重新预览")
                self.gateway.publish(output, key, backend)
            return key

    def prepare_import(self, source: Path, backend, *, destination: str | None = None) -> tuple[str | None, str | None]:
        try:
            return self.prepare(source, backend, destination=destination), None
        except InterruptedError:
            raise
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            return None, f"首帧生成失败：{str(exc)[-300:]}"

    def save(self, recording_id: int, source: Path | None = None, backend=None):
        g = self.gateway
        backend = backend or g.backend
        row = g.recording(recording_id)
        if row["thumbnail_path"]:
            return
        if source is None and isinstance(backend, LocalBackend) and row["remote_path"]:
            source = backend._path(row["remote_path"])
        if not backend or source is None:
            return
        key, error = self.prepare_import(source, backend)
        g.db.execute("UPDATE recordings SET thumbnail_path=?,thumbnail_error=? WHERE id=?", (key, error, recording_id))

    @staticmethod
    def read(key: str, backend) -> bytes:
        if not backend:
            raise FileNotFoundError("尚未配置缩略图存储")
        with backend.reader(key) as stream:
            return stream.read(512 * 1024)

    def recording(self, recording_id: int) -> bytes:
        g = self.gateway
        row = g.recording(recording_id)
        if not row["thumbnail_path"] and not row["thumbnail_error"] and row["state"] == "stored":
            self.save(recording_id)
            row = g.recording(recording_id)
        if not row["thumbnail_path"]:
            raise FileNotFoundError(row["thumbnail_error"] or "录像封存后生成首帧预览")
        return self.read(row["thumbnail_path"], g.backend)
