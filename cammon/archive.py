import hashlib
import json
import logging
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from cammon.naming import TZ, day_slices
from cammon.storage import CHUNK, file_hash

log = logging.getLogger(__name__)


class Archiver:
    def __init__(self, gateway):
        self.gateway = gateway

    def enqueue(self, now: float | None = None) -> int:
        g = self.gateway
        today = datetime.fromtimestamp(now or time.time(), TZ).date()
        groups = {}
        with g.lock:
            for device in g.devices():
                cutoff = (today - timedelta(days=device["retention_days"] - 1)).isoformat()
                rows = g.db.all("SELECT * FROM recordings WHERE device_id=? AND kind='original' AND state='stored' "
                                "AND end>start ORDER BY start,id", (device["id"],))
                for row in rows:
                    for day, offset, length in day_slices(row["start"], row["end"]):
                        if day >= cutoff or g.db.one("SELECT * FROM coverage WHERE recording_id=? AND day=?", (row["id"], day)):
                            continue
                        # A queued/failed job owns its slices until successfully committed.
                        jobs = g.db.all("SELECT sources FROM archive_jobs WHERE device_id=? AND channel=? AND day=? "
                                        "AND status!='done'", (device["id"], row["channel"], day))
                        if any(row["id"] in [s["id"] for s in json.loads(j["sources"])] for j in jobs):
                            continue
                        key = (device["id"], row["channel"], day)
                        groups.setdefault(key, []).append({"id": row["id"], "offset": offset, "length": length})
            for (device_id, channel, day), sources in groups.items():
                manifest = json.dumps(sources, sort_keys=True, separators=(",", ":"))
                job_id = hashlib.sha256(f"{device_id}/{channel}/{day}/{manifest}".encode()).hexdigest()[:32]
                now_value = time.time()
                g.db.execute("INSERT OR IGNORE INTO archive_jobs(id,device_id,channel,day,sources,created_at,updated_at) "
                             "VALUES (?,?,?,?,?,?,?)", (job_id, device_id, channel, day, manifest, now_value, now_value))
        return len(groups)

    def probe(self, path: Path) -> dict:
        result = self._run_process([
            self.gateway.settings.ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path),
        ], timeout=120)
        info = json.loads(result.stdout)
        videos = [stream for stream in info["streams"] if stream["codec_type"] == "video"]
        if not videos:
            raise ValueError("文件没有可解码的视频轨道")
        return {**videos[0], "duration": float(info["format"]["duration"])}

    def _ffmpeg(self, arguments: list[str]):
        command = [self.gateway.settings.ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-y"] + arguments
        self._run_process(command, timeout=24 * 3600, check_space=True)

    def _run_process(self, command, timeout, check_space=False):
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
            deadline = time.monotonic() + timeout
            try:
                while True:
                    if self.gateway.stopping.is_set():
                        raise InterruptedError("服务正在停止，转码任务将重新入队")
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

    def process(self, job_id: str) -> bool:
        g = self.gateway
        with g.lock:
            job = g.db.one("SELECT * FROM archive_jobs WHERE id=?", (job_id,))
            backend = g.backend
            if not job or job["status"] == "done" or not backend:
                return False
            backend.stopping = g.stopping
            g.db.execute("UPDATE archive_jobs SET status='running',error=NULL,updated_at=?,attempts=attempts+1 WHERE id=?",
                         (time.time(), job_id))
        directory = g.work / job_id
        directory.mkdir(exist_ok=True)
        output = directory / "archive.mp4"
        remote = f"archives/{job['device_id']}/{job['channel']}/{job['day']}/{job_id}.mp4"
        sources = json.loads(job["sources"])
        try:
            already_uploaded = bool(job["output_sha"] and backend.exists(remote) and
                                    backend.stat(remote) == job["output_size"] and backend.sha256(remote) == job["output_sha"])
            if already_uploaded:
                size, digest = job["output_size"], job["output_sha"]
                duration = sum(s["length"] for s in sources) / 60
            else:
                parts = []
                width = height = None
                expected = 0
                for i, source in enumerate(sources):
                    row = g.recording(source["id"])
                    source_path = directory / f"input-{i}.mp4"
                    with backend.reader(row["remote_path"]) as stream, source_path.open("wb") as local:
                        while data := stream.read(CHUNK):
                            backend.check_cancelled()
                            with g.lock:
                                g.reserve(len(data))
                                local.write(data)
                    metadata = self.probe(source_path)
                    if abs(metadata["duration"] - (row["end"] - row["start"])) > 2:
                        raise ValueError(f"录像时长与文件名时间区间不符: {row['path']}")
                    length = min(source["length"], metadata["duration"] - source["offset"])
                    if length <= 0:
                        raise ValueError("录像缺少此日期对应的内容")
                    if width is None:
                        width, height = int(metadata["width"]) // 2 * 2, int(metadata["height"]) // 2 * 2
                    part = directory / f"part-{i}.mp4"
                    filters = f"setpts=(PTS-STARTPTS)/60,fps=25,scale={width}:{height}:force_original_aspect_ratio=decrease," \
                              f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,format=yuv420p"
                    self._ffmpeg([
                        "-threads", str(g.settings.ffmpeg_threads), "-ss", str(source["offset"]),
                        "-t", str(length), "-i", str(source_path), "-map", "0:v:0", "-an", "-vf", filters,
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                        "-threads", str(g.settings.ffmpeg_threads), "-map_metadata", "-1", str(part),
                    ])
                    self.probe(part)
                    source_path.unlink()
                    parts.append(part)
                    expected += length / 60
                manifests = g.settings.data_dir / "archive-manifests"
                manifests.mkdir(mode=0o700, exist_ok=True)
                manifest = manifests / f"{job_id}.txt"
                paths = [str(part.resolve()).replace("'", "'\\''") for part in parts]
                manifest.write_text("".join(f"file '{path}'\n" for path in paths))
                try:
                    self._ffmpeg(["-f", "concat", "-safe", "0", "-i", str(manifest), "-c", "copy", "-an",
                                  "-map_metadata", "-1", "-movflags", "+faststart", str(output)])
                finally:
                    manifest.unlink(missing_ok=True)
                metadata = self.probe(output)
                duration = metadata["duration"]
                if abs(duration - expected) > max(0.2, len(parts) / 25 + 0.1):
                    raise ValueError("归档视频时长校验失败")
                if metadata["codec_name"] != "h264":
                    raise ValueError("归档视频编码校验失败")
                size, digest = output.stat().st_size, file_hash(output, g.stopping)
                g.db.execute("UPDATE archive_jobs SET output_size=?,output_sha=? WHERE id=?", (size, digest, job_id))
                g.publish(output, remote, backend)
            start = datetime.combine(datetime.fromisoformat(job["day"]).date(), datetime.min.time(), TZ).timestamp()
            with g.lock:
                g.db.execute("BEGIN IMMEDIATE")
                try:
                    cursor = g.db.execute(
                        "INSERT INTO recordings(device_id,path,channel,start,end,day,kind,state,size,created_at,last_write,"
                        "remote_path,sha256,duration,job_id) VALUES (?,?,?,?,?,?,'archive','stored',?,?,?,?,?,?,?)",
                        (job["device_id"], f"{job['channel']}_{job['day']}_{job_id}.mp4", job["channel"], start,
                         start + 86400, job["day"], size, time.time(), time.time(), remote, digest, duration, job_id),
                    )
                    archive_id = cursor.lastrowid
                    for source in sources:
                        g.db.execute("INSERT OR IGNORE INTO coverage VALUES (?,?,?)", (source["id"], job["day"], job_id))
                    g.db.execute("UPDATE archive_jobs SET status='done',archive_id=?,updated_at=?,error=NULL WHERE id=?",
                                 (archive_id, time.time(), job_id))
                    g.db.execute("COMMIT")
                except Exception:
                    g.db.execute("ROLLBACK")
                    raise
            shutil.rmtree(directory)
            self.collect_originals()
            return True
        except Exception as exc:
            if g.db.one("SELECT status FROM archive_jobs WHERE id=?", (job_id,))["status"] == "done":
                # Publication/coverage committed successfully; cleanup cannot invalidate it.
                log.exception("Archive %s was published, but cleanup needs a retry", job_id)
                return True
            g.db.execute("UPDATE archive_jobs SET status='failed',error=?,retry_at=?,updated_at=? WHERE id=?",
                         (str(exc), time.time() + 300, time.time(), job_id))
            log.exception("Archive job %s failed", job_id)
            # Keep only the verified publication candidate, not huge source downloads, on failure.
            verified = g.db.one("SELECT output_sha FROM archive_jobs WHERE id=?", (job_id,))["output_sha"]
            for path in directory.iterdir():
                if path.name != "archive.mp4" or not verified:
                    path.unlink(missing_ok=True)
            return False

    def collect_originals(self):
        g = self.gateway
        if not g.backend:
            return
        backend = g.backend
        for directory in g.work.iterdir():
            if directory.is_dir() and g.db.one("SELECT id FROM archive_jobs WHERE id=? AND status='done'", (directory.name,)):
                shutil.rmtree(directory, ignore_errors=True)
        for row in g.db.all("SELECT * FROM recordings WHERE kind='original' AND state='stored' AND end>start AND retry_at<=?",
                            (time.time(),)):
            if g.stopping.is_set():
                return
            with g.lock:
                if any(h.recording_id == row["id"] for h in g.handles.values()):
                    continue
                days = day_slices(row["start"], row["end"])
                coverage = [g.db.one("SELECT * FROM coverage WHERE recording_id=? AND day=?", (row["id"], day))
                            for day, _, _ in days]
                if not days or not all(coverage):
                    continue
                if g.metadata.configured:
                    # A reboot may lose memory. Historical originals stay until
                    # the permanent archive and every covered day are saved in PG.
                    confirmed = True
                    for item in coverage:
                        job = g.db.one("SELECT * FROM archive_jobs WHERE id=?", (item["job_id"],))
                        if (not job or job["status"] != "done" or job["archive_id"] is None
                            or g.db.metadata_pending("coverage", [row["id"], item["day"]])
                            or g.db.metadata_pending("archive_jobs", [item["job_id"]])
                            or g.db.metadata_pending("recordings", [job["archive_id"]])):
                            confirmed = False
                            break
                    if not confirmed:
                        continue
                g.db.execute("UPDATE recordings SET state='deleting' WHERE id=?", (row["id"],))
            # Network I/O cannot hold the camera write/namespace lock during an outage.
            try:
                try:
                    backend.remove(row["remote_path"])
                except FileNotFoundError:
                    pass
                with g.lock:
                    device = g.device(row["device_id"], enabled=False)
                    g._projection(device, row["path"]).unlink(missing_ok=True)
                    g.db.execute("UPDATE recordings SET state='deleted',error=NULL WHERE id=?", (row["id"],))
            except Exception as exc:
                g.db.execute("UPDATE recordings SET state='stored',error=?,retry_at=? WHERE id=?",
                             (str(exc), time.time() + 30, row["id"]))
                continue


class Worker:
    def __init__(self, gateway):
        self.gateway = gateway
        self.archiver = Archiver(gateway)
        self.stopping = gateway.stopping
        self.thread = threading.Thread(target=self.run, name="retention-worker", daemon=True)

    def start(self):
        self.thread.start()

    def tick(self):
        g = self.gateway
        g.seal_candidates()
        if g.backend:
            for row in g.db.all("SELECT id FROM recordings WHERE state='pending' AND retry_at<=?", (time.time(),)):
                if self.stopping.is_set():
                    return
                g.flush_one(row["id"])
            now = datetime.now(TZ)
            last = g.get_setting("last_archive_enqueue")
            if now.hour >= 2 and last != now.date().isoformat():
                self.archiver.enqueue()
                g.set_setting("last_archive_enqueue", now.date().isoformat())
            job = g.db.one("SELECT id FROM archive_jobs WHERE status IN ('queued','failed') AND retry_at<=? "
                           "ORDER BY created_at LIMIT 1", (time.time(),))
            if job and not self.stopping.is_set():
                self.archiver.process(job["id"])
            self.archiver.collect_originals()

    def run(self):
        while not self.stopping.is_set():
            try:
                self.tick()
                self.gateway.worker_error = None
            except Exception as exc:
                self.gateway.worker_error = str(exc)
                log.exception("Background worker failed")
            self.stopping.wait(self.gateway.settings.worker_interval_seconds)

    def stop(self):
        self.stopping.set()
        # Finish/cancel the current bounded network operation before SQLite is closed.
        self.thread.join()
