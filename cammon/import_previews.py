"""Paged import confirmation snapshots; thumbnails are requested individually."""

import threading
import time
import uuid

from cammon.importer import RecordingImporter


class ImportPreviews:
    def __init__(self, gateway):
        self.gateway = gateway
        self.lock = threading.Lock()
        self.previews: dict[str, tuple[float, RecordingImporter, dict]] = {}

    def create(self, device_id: str, folder: str, recursive: bool) -> dict:
        importer = RecordingImporter(self.gateway)
        report = importer.scan(device_id, folder, recursive, item_limit=None)
        preview_id = uuid.uuid4().hex
        with self.lock:
            self.previews = {key: value for key, value in self.previews.items()
                             if value[0] > time.monotonic() - 7200}
            while len(self.previews) >= 10:
                del self.previews[next(iter(self.previews))]
            self.previews[preview_id] = (time.monotonic(), importer, report)
        return self.page(preview_id)

    def get(self, preview_id: str) -> tuple[RecordingImporter, dict]:
        with self.lock:
            preview = self.previews.get(preview_id)
            if not preview or preview[0] < time.monotonic() - 7200:
                self.previews.pop(preview_id, None)
                raise FileNotFoundError("导入预览已过期，请重新预览")
            _, importer, report = preview
        if importer.backend is not self.gateway.backend:
            raise ValueError("存储配置已改变，请重新预览")
        return importer, report

    def page(self, preview_id: str, offset: int = 0, limit: int = 25) -> dict:
        _, report = self.get(preview_id)
        return {**report, "preview_id": preview_id, "offset": offset, "limit": limit,
                "items": report["items"][offset:offset + limit]}

    def thumbnail(self, preview_id: str, index: int) -> bytes:
        importer, report = self.get(preview_id)
        if index >= len(report["items"]):
            raise FileNotFoundError("预览文件不存在")
        item = report["items"][index]
        if not item.get("thumbnail_key"):
            raise ValueError(item["reason"] or "该文件没有视频首帧")
        folder = importer.folder(report["folder"])
        source = folder / item["path"]
        importer.folder(str(source.parent))
        key = self.gateway.thumbnails.prepare(source, importer.backend, item["thumbnail_key"])
        return self.gateway.thumbnails.read(key, importer.backend)
