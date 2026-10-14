"""Resolve disk paths and reserve import trees for a single camera."""

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from cammon.storage import LocalBackend

FOLDER_PREFIX = "folder-owner:"
COPY_PREFIX = "import-copy:"


def folder_key(path: str) -> str:
    return FOLDER_PREFIX + hashlib.sha256(path.encode()).hexdigest()


class OwnershipConflictError(ValueError):
    pass


@dataclass
class OwnershipSnapshot:
    devices: dict
    directories: list[dict]
    files_by_path: dict
    directories_by_path: dict
    directory_paths: set
    descendant_owners: dict


class PathOwnership:
    def __init__(self, gateway):
        self.gateway = gateway
        self.backend = gateway.backend
        if not isinstance(self.backend, LocalBackend):
            raise ValueError("路径归属查询需要配置本地磁盘存储")
        self.root = self.backend.root

    def path(self, value: str) -> Path:
        path = Path(value)
        if (not path.is_absolute() or ".." in path.parts
                or any(c in value for c in ("\n", "\r", "\x00"))):
            raise ValueError("请选择本地存储目录内的绝对路径")
        if not path.is_relative_to(self.root):
            raise ValueError("查询路径必须位于配置的本地存储目录内")
        for parent in (path, *path.parents):
            if parent == self.root:
                break
            if parent.is_symlink():
                raise ValueError("查询路径不能使用符号链接")
        return path

    def snapshot(self) -> OwnershipSnapshot:
        g = self.gateway
        with g.lock:
            devices = {device["id"]: device for device in g.devices()}
            saved_rows = g.db.all("SELECT key,value FROM settings WHERE key LIKE ? OR key LIKE ?",
                                  (FOLDER_PREFIX + "%", COPY_PREFIX + "%"))
            recordings = g.db.all("SELECT id,device_id,remote_path,thumbnail_path FROM recordings "
                                  "WHERE state!='deleted'")
        # Capture metadata under the lock, then build the shared browse index
        # without blocking camera writes for the CPU work.
        directories = []
        files = []
        for device_id in devices:
            for prefix in ("originals", "archives"):
                directories.append(dict(path=PurePosixPath(prefix, device_id), device_id=device_id,
                                        source="managed"))
        for saved in saved_rows:
            row = json.loads(saved["value"])
            if saved["key"].startswith(FOLDER_PREFIX):
                directories.append(dict(path=PurePosixPath(row["path"]), device_id=row["device_id"],
                                        source="import"))
            else:
                files.append(dict(path=PurePosixPath(row["remote_path"]), device_id=row["device_id"],
                                  source="copy", recording_id=row["recording_id"]))
        for row in recordings:
            for column, source in (("remote_path", "recording"), ("thumbnail_path", "thumbnail")):
                if row[column]:
                    files.append(dict(path=PurePosixPath(row[column]), device_id=row["device_id"],
                                      source=source, recording_id=row["id"]))
        # Older installations have file associations without explicit tree
        # reservations. Treat their source parents as owned too, including
        # linked copies and originals whose bytes have been archived away.
        for row in files:
            if (row["source"] != "thumbnail" and row["path"].parts[0] not in ("originals", "archives")
                    and row["path"].parent != PurePosixPath(".")):
                directories.append(dict(path=row["path"].parent, device_id=row["device_id"],
                                        source="legacy"))
        directories = list({(row["path"], row["device_id"], row["source"]): row
                            for row in directories}.values())
        files_by_path = defaultdict(list)
        directories_by_path = defaultdict(list)
        directory_paths = set()
        descendant_owners = defaultdict(set)
        for row in files:
            files_by_path[row["path"]].append(row)
        for row in directories:
            directories_by_path[row["path"]].append(row)
            directory_paths.update((row["path"], *row["path"].parents))
        for row in directories + files:
            for ancestor in (row["path"], *row["path"].parents):
                descendant_owners[ancestor].add(row["device_id"])
        return OwnershipSnapshot(devices, directories, files_by_path, directories_by_path,
                                 directory_paths, descendant_owners)

    def describe(self, value: str, snapshot=None) -> dict:
        path = self.path(value)
        key = PurePosixPath(path.relative_to(self.root).as_posix())
        snapshot = snapshot if snapshot is not None else self.snapshot()
        exact = snapshot.files_by_path.get(key, [])
        covering = [row for ancestor in (key, *key.parents)
                    for row in snapshot.directories_by_path.get(ancestor, [])]
        is_directory = path.is_dir() or key in snapshot.directory_paths
        # A registered file keeps its precise camera association even when its
        # legacy parent contains conflicting associations for other files.
        owners = {row["device_id"] for row in (exact if exact and not is_directory else exact + covering)}
        children = snapshot.descendant_owners.get(key, set()) if is_directory else set()
        conflict = len(owners) > 1 or bool(owners and children - owners)
        status = "conflict" if conflict else "owned" if owners else "container" if children else "unassigned"
        return dict(root=str(self.root), path=str(path), exists=path.exists(),
                    kind="directory" if is_directory else "file" if path.is_file() or exact else "missing",
                    status=status, owners=[dict(device_id=device_id, device_name=snapshot.devices.get(device_id, {}).get(
                        "name", device_id)) for device_id in sorted(owners | children if conflict else owners or children)],
                    bindings=[dict(path=str(self.root / row["path"]), device_id=row["device_id"],
                                   source=row["source"]) for row in covering],
                    recording_ids=sorted({row["recording_id"] for row in exact}))

    def check(self, folder: Path, device_id: str):
        """Check both ancestors and descendants, independently of scan depth."""
        key = PurePosixPath(folder.relative_to(self.root).as_posix())
        if folder == self.root:
            raise ValueError("本地存储根目录是公共容器，请选择一个独立的导入文件夹")
        snapshot = self.snapshot()
        for row in snapshot.directories:
            if row["device_id"] != device_id and (
                    key.is_relative_to(row["path"]) or row["path"].is_relative_to(key)):
                name = snapshot.devices.get(row["device_id"], {}).get("name", row["device_id"])
                raise OwnershipConflictError(f"文件夹 {self.root / row['path']} 已归属摄像机“{name}”，"
                                             "不能将该目录或其父子目录归入其他摄像机")

    def claim(self, folder: Path, device_id: str):
        g = self.gateway
        with g.lock:
            if g.backend is not self.backend:
                raise ValueError("存储配置已改变，请重新预览")
            g.device(device_id, enabled=False)
            # A previously checked reservation already protects its complete
            # subtree. Avoid loading the recording index for every source file.
            for parent in (folder, *folder.parents):
                if parent == self.root:
                    break
                saved = g.get_setting(folder_key(parent.relative_to(self.root).as_posix()))
                if saved and saved["device_id"] == device_id:
                    return
            self.check(folder, device_id)
            path = folder.relative_to(self.root).as_posix()
            key = folder_key(path)
            if not g.get_setting(key):
                g.set_setting(key, dict(path=path, device_id=device_id))
