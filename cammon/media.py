"""Video metadata and cancellable media processes shared by ingestion and archiving."""

import json
import math
import subprocess
import time
from pathlib import Path


class MediaTools:
    def __init__(self, gateway):
        self.gateway = gateway

    def probe(self, path: Path) -> dict:
        try:
            result = self._run_process([
                self.gateway.settings.ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path),
            ], timeout=120)
            info = json.loads(result.stdout)
            videos = [stream for stream in info["streams"] if stream["codec_type"] == "video"]
            if not videos:
                raise ValueError("文件没有可解码的视频轨道")
            duration = float(info["format"]["duration"])
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError("录像时长必须是有限的正数")
            return {**videos[0], "duration": duration}
        except (KeyError, TypeError, ValueError, RuntimeError, TimeoutError) as exc:
            raise ValueError(f"无法读取录像时长: {exc}") from exc

    def _run_process(self, command, timeout, check_space=False):
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
            deadline = time.monotonic() + timeout
            try:
                while True:
                    if self.gateway.stopping.is_set():
                        raise InterruptedError("服务正在停止，视频处理已取消")
                    if time.monotonic() >= deadline:
                        raise TimeoutError("视频处理超时")
                    if check_space and self.gateway.available() == 0:
                        raise OSError("本地缓存空间不足，归档源录像已保留")
                    try:
                        stdout, stderr = process.communicate(timeout=0.5)
                        break
                    except subprocess.TimeoutExpired:
                        continue
                if process.returncode:
                    raise RuntimeError(stderr[-3000:] or "视频处理失败")
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
