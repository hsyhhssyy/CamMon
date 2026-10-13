"""Adopt historical recordings already placed on the configured local disk."""

import hashlib
import os
import stat
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from cammon.media import MediaTools
from cammon.naming import TZ, day_slices, is_minute_recording, recording_name
from cammon.storage import LocalBackend, file_hash, safe_key

COPY_PREFIX = "import-copy:"


def copy_key(remote_path: str) -> str:
    return COPY_PREFIX + hashlib.sha256(remote_path.encode()).hexdigest()


class RecordingImporter:
    sample_limit = 100

    def __init__(self, gateway):
        self.gateway = gateway
        self.backend = gateway.backend
        if not isinstance(self.backend, LocalBackend):
            raise ValueError("旧录像导入需要先配置本地磁盘存储，再把录像放到该磁盘目录下")
        self.root = self.backend.root

    def folder(self, value: str | None = None) -> Path:
        path = Path(value) if value else self.root
        if (not path.is_absolute() or ".." in path.parts
                or any(c in str(path) for c in ("\n", "\r", "\x00"))):
            raise ValueError("请选择本地存储目录内的绝对路径")
        if not path.is_relative_to(self.root):
            raise ValueError("导入文件夹必须位于配置的本地存储目录内")
        for parent in (path, *path.parents):
            if parent == self.root:
                break
            if parent.is_symlink():
                raise ValueError("导入文件夹不能使用符号链接")
        if self.excluded(path):
            raise ValueError("不能导入 CamMon 的原录像、归档或运行缓存目录")
        if not path.is_dir():
            raise FileNotFoundError("导入文件夹不存在或不是目录")
        return path

    def excluded(self, path: Path) -> bool:
        relative = path.relative_to(self.root)
        if relative.parts and relative.parts[0] in ("originals", "archives"):
            return True
        return any(path.is_relative_to(private.resolve()) for private in (
            self.gateway.settings.data_dir, self.gateway.settings.cache,
        ))

    def folders(self, value: str | None = None) -> dict:
        path = self.folder(value)
        children = [child for child in path.iterdir()
                    if not child.is_symlink() and child.is_dir() and not self.excluded(child)]
        return {"root": str(self.root), "path": str(path),
                "parent": str(path.parent) if path != self.root else None,
                "folders": [{"name": child.name, "path": str(child)}
                            for child in sorted(children, key=lambda child: child.name)]}

    def files(self, folder: Path, recursive: bool):
        def scan_error(error):
            raise error

        for directory, children, names in os.walk(folder, followlinks=False, onerror=scan_error):
            parent = Path(directory)
            # Do not traverse a directory replaced with a symlink during a scan.
            self.folder(str(parent))
            children[:] = sorted(child for child in children
                                 if not (parent / child).is_symlink() and not self.excluded(parent / child))
            for name in sorted(names):
                self.backend.check_cancelled()
                yield parent / name
            if not recursive:
                break

    def candidate(self, source: Path) -> dict:
        info = source.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("跳过符号链接或非普通文件")
        if info.st_size == 0:
            raise ValueError("文件为空")
        # Source trees can be arbitrarily deep; map each source parent to a
        # stable two-level import directory in the camera namespace.
        remote = safe_key(source.relative_to(self.root).as_posix())
        group = hashlib.sha256(source.parent.relative_to(self.root).as_posix().encode()).hexdigest()[:16]
        path = f"imports/{group}/{source.name}"
        channel, start, end, day = recording_name(source.name, source.parent.name)
        duration = None
        if is_minute_recording(source.name):
            duration = MediaTools(self.gateway).probe(source)["duration"]
            end = start + duration
            after = source.lstat()
            if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
                    after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise ValueError("文件在读取时长时发生变化，请停止写入后重新导入")
        if end == start:
            raise ValueError("录像起止时间相同，无法生成加速视频")
        return dict(path=path, channel=channel, start=start, end=end, day=day, size=info.st_size, duration=duration,
                    remote_path=remote, last_write=info.st_mtime)

    def existing(self, device_id: str, row: dict) -> str | None:
        g = self.gateway
        existing = g.db.one("SELECT device_id FROM recordings WHERE remote_path=? AND kind='original' LIMIT 1",
                            (row["remote_path"],))
        if existing:
            return "该文件已归入此摄像机" if existing["device_id"] == device_id else "该文件已归入其他摄像机"
        copy = g.get_setting(copy_key(row["remote_path"]))
        if copy:
            if copy["device_id"] != device_id:
                return "该副本已归入其他摄像机"
            if copy.get("error"):
                return "副本已关联，但清理失败：" + copy["error"]
            return "该副本已关联现有录像，将在归档成功后清理"
        if g.by_path(device_id, row["path"]):
            return "摄像机中已存在同名录像"
        return None

    def matches(self, device_id: str, row: dict) -> list[dict]:
        matches = self.gateway.db.all(
            "SELECT * FROM recordings WHERE device_id=? AND channel=? AND start=? "
            "AND kind='original' AND state!='deleted' ORDER BY id",
            (device_id, row["channel"], row["start"]),
        )
        # Minute names identify the start independently of video duration, so
        # conflicting contents must not bypass copy checks by changing the end.
        # A live minute recording's nominal end is not authoritative either.
        return [match for match in matches if match["end"] == row["end"] or (
            is_minute_recording(match["path"]) and (
                is_minute_recording(row["path"]) or match["state"] in ("cached", "pending", "uploading")
            )
        )]

    def duplicate(self, device_id: str, source: Path, row: dict) -> tuple[dict, str] | None:
        matches = self.matches(device_id, row)
        if not matches:
            return None
        digest = None
        for existing in matches:
            if existing["state"] not in ("stored", "virtual") or existing["size"] != row["size"]:
                continue
            digest = digest or file_hash(source, self.gateway.stopping)
            # A virtual recording has no original bytes. Its previously verified
            # hash can identify a copy, but a filename alone cannot justify deletion.
            if existing["state"] == "virtual":
                expected = existing["sha256"]
            else:
                expected = self.backend.sha256(existing["remote_path"])
                if existing["sha256"] and expected != existing["sha256"]:
                    raise ValueError("摄像机中的原录像内容已改变，已保留导入源文件")
            if digest == expected:
                if self.candidate(source) != row:
                    raise ValueError("文件在校验时发生变化，请停止写入后重新导入")
                return existing, digest
        if any(existing["state"] not in ("stored", "virtual") for existing in matches):
            raise ValueError("摄像机中的同名录像尚未封存或正在处理，请稍后重新导入")
        raise ValueError("摄像机中存在同名录像，但内容不同或无法核对，已保留导入源文件")

    def link_copy(self, device_id: str, source: Path, row: dict, duplicate: tuple[dict, str]) -> bool:
        g = self.gateway
        self.folder(str(source.parent))
        if self.candidate(source) != row:
            raise ValueError("文件在校验后发生变化，请停止写入后重新导入")
        existing, digest = duplicate
        with g.lock:
            if g.backend is not self.backend:
                raise ValueError("存储配置已改变，请重新预览")
            if self.existing(device_id, row):
                return False
            current = g.recording(existing["id"])
            if (current["state"] not in ("stored", "virtual") or any(
                    current[key] != existing[key] for key in ("device_id", "remote_path", "size", "start", "end"))):
                raise ValueError("摄像机中的录像发生变化，请重新预览")
            g.db.execute("BEGIN IMMEDIATE")
            try:
                g.db.execute("UPDATE recordings SET sha256=? WHERE id=?", (digest, existing["id"]))
                g.set_setting(copy_key(row["remote_path"]), {
                    "recording_id": existing["id"], "device_id": device_id,
                    "remote_path": row["remote_path"], "size": row["size"], "sha256": digest,
                    "error": None, "retry_at": 0,
                })
                g.db.execute("COMMIT")
            except BaseException:
                g.db.execute("ROLLBACK")
                raise
        return True

    def adopt(self, device_id: str, source: Path, row: dict) -> str | None:
        g = self.gateway
        # Filesystem I/O and tree walking happen without the camera write lock.
        self.folder(str(source.parent))
        if self.candidate(source) != row:
            raise ValueError("文件在扫描后发生变化，请停止写入后重新导入")
        with g.lock:
            if g.backend is not self.backend:
                raise ValueError("存储配置已改变，请重新预览")
            reason = self.existing(device_id, row)
            if reason:
                return reason
            if self.matches(device_id, row):
                raise ValueError("摄像机中刚出现同名录像，请重新预览以核对副本")
            device = g.device(device_id, enabled=False)
            g.db.execute("BEGIN IMMEDIATE")
            try:
                first, second, _ = row["path"].split("/")
                for directory in (first, f"{first}/{second}"):
                    g.db.execute("INSERT OR IGNORE INTO directories(device_id,path,modified_at) VALUES (?,?,?)",
                                 (device_id, directory, time.time()))
                    g._mirror_directory(device, directory)
                g._mirror_file(device, row["path"])
                g.db.execute(
                    "INSERT INTO recordings(device_id,path,channel,start,end,day,state,size,created_at,last_write,"
                    "remote_path,duration) VALUES (:device_id,:path,:channel,:start,:end,:day,'stored',:size,:created_at,"
                    ":last_write,:remote_path,:duration)",
                    {**row, "device_id": device_id, "created_at": time.time()},
                )
                g.db.execute("COMMIT")
            except BaseException:
                g.db.execute("ROLLBACK")
                raise
        return None

    def scan(self, device_id: str, value: str, recursive: bool = True, *, commit: bool = False,
             progress: Callable[[dict, str | None, int | None], None] | None = None) -> dict:
        device = self.gateway.device(device_id, enabled=False)
        folder = self.folder(value)
        cutoff = (datetime.now(TZ).date() - timedelta(days=device["retention_days"] - 1)).isoformat()
        self.backend.stopping = self.gateway.stopping
        result = dict(folder=str(folder), device_id=device_id, recursive=recursive, total=0,
                      importable=0, imported=0, duplicates=0, copies=0, skipped=0, failed=0,
                      bytes=0, expired=0, items=[])
        total_files = None
        if progress:
            progress(result, None, None)
            total_files = 0
            for source in self.files(folder, recursive):
                progress(result, source.relative_to(folder).as_posix(), None)
                total_files += 1
            progress(result, None, total_files)
        for source in self.files(folder, recursive):
            if progress:
                # Publish the current file before probing video or hashing copies.
                total_files = max(total_files, result["total"] + 1)
                progress(result, source.relative_to(folder).as_posix(), total_files)
            result["total"] += 1
            item = {"path": source.relative_to(folder).as_posix(), "status": "skipped", "reason": None}
            try:
                row = self.candidate(source)
                reason = self.existing(device_id, row)
                duplicate = self.duplicate(device_id, source, row) if not reason else None
            except (OSError, ValueError) as exc:
                result["skipped"] += 1
                item["reason"] = str(exc)
            else:
                item.update(day=row["day"], channel=row["channel"], size=row["size"])
                try:
                    if duplicate:
                        result["copies"] += int(self.link_copy(device_id, source, row, duplicate)) if commit else 1
                        reason = "已关联现有录像，归档成功后清理副本" if commit else "内容一致，可关联现有录像并随归档清理副本"
                    elif not reason and commit:
                        reason = self.adopt(device_id, source, row)
                    if reason:
                        result["duplicates"] += 1
                        item.update(status="duplicate", reason=reason)
                    else:
                        result["importable"] += 1
                        result["imported"] += int(commit)
                        result["bytes"] += row["size"]
                        expired = any(day < cutoff for day, _, _ in day_slices(row["start"], row["end"]))
                        result["expired"] += int(expired)
                        item.update(status="imported" if commit else "ready", expired=expired)
                except (OSError, ValueError) as exc:
                    result["failed"] += 1
                    item.update(status="failed", reason=str(exc))
            if len(result["items"]) < self.sample_limit:
                result["items"].append(item)
            if progress:
                progress(result, None, total_files)
        return result
