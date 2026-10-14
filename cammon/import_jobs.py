"""Process-local import tasks whose progress survives browser disconnections."""

import copy
import logging
import threading
import time
import uuid

from cammon.importer import RecordingImporter
from cammon.ownership import PathOwnership

log = logging.getLogger(__name__)


class ImportBusyError(ValueError):
    pass


class ImportJobs:
    history_limit = 10

    def __init__(self, gateway):
        self.gateway = gateway
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.jobs: dict[str, dict] = {}
        self.thread: threading.Thread | None = None

    def start(self, device_id: str, folder: str, recursive: bool = True) -> dict:
        importer = RecordingImporter(self.gateway)
        device = self.gateway.device(device_id, enabled=False)
        folder = str(importer.folder(folder))
        with self.lock:
            if self.stopping.is_set():
                raise ImportBusyError("服务正在停止，暂不能开始导入")
            active = next((job for job in self.jobs.values() if job["status"] == "running"), None)
            if active:
                if (active["device_id"], active["folder"], active["recursive"]) == (device_id, folder, recursive):
                    return copy.deepcopy(active)
                raise ImportBusyError("已有导入任务正在运行，请在旧录像导入页面查看进度，完成后再开始新任务")
            # Reserve synchronously so conflicting submissions fail before a
            # worker can partially adopt files from this tree.
            PathOwnership(self.gateway).claim(importer.folder(folder), device_id)
            job = dict(id=uuid.uuid4().hex, device_id=device_id, device_name=device["name"], folder=folder,
                       recursive=recursive, status="running", created_at=time.time(), finished_at=None,
                       total_files=None, current_path=None, report=None, error=None)
            self.jobs[job["id"]] = job
            while len(self.jobs) > self.history_limit:
                del self.jobs[next(iter(self.jobs))]
            self.thread = threading.Thread(target=self._run, args=(importer, job["id"]),
                                           name="cammon-import", daemon=True)
            self.thread.start()
            return copy.deepcopy(job)

    def list(self) -> list[dict]:
        with self.lock:
            return copy.deepcopy(list(reversed(self.jobs.values())))

    def get(self, job_id: str) -> dict:
        with self.lock:
            if job_id not in self.jobs:
                raise FileNotFoundError("导入任务不存在或已过期")
            return copy.deepcopy(self.jobs[job_id])

    def _progress(self, job_id: str, report: dict, current_path: str | None, total_files: int | None):
        with self.lock:
            self.jobs[job_id].update(report={**report, "items": list(report["items"])},
                                     current_path=current_path, total_files=total_files)
        if self.stopping.is_set():
            raise InterruptedError("服务停止，导入已中断，请重新发起导入")

    def _run(self, importer: RecordingImporter, job_id: str):
        job = self.get(job_id)
        try:
            report = importer.scan(job["device_id"], job["folder"], job["recursive"], commit=True,
                                   progress=lambda *args: self._progress(job_id, *args))
        except Exception as exc:
            if not isinstance(exc, InterruptedError):
                log.exception("Recording import failed")
            with self.lock:
                self.jobs[job_id].update(status="failed", error=str(exc), finished_at=time.time())
        else:
            with self.lock:
                self.jobs[job_id].update(status="completed", report=report, total_files=report["total"],
                                         current_path=None, finished_at=time.time())

    def stop(self):
        with self.lock:
            self.stopping.set()
            thread = self.thread
        if thread:
            thread.join()
