import errno
import json
import os
import secrets
import shutil
import stat
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from pydantic import SecretStr

from cammon.config import Settings
from cammon.database import Database
from cammon.metadata import MetadataSynchronizer
from cammon.naming import directory_path, path_parts, recording_path
from cammon.security import Secrets, password_hash
from cammon.storage import Backend, file_hash, make_backend, reject_self_storage


@dataclass
class OpenFile:
    recording_id: int
    writable: bool
    session: str
    fd: int | None = None
    remote: BinaryIO | None = None
    closed: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock)


class Gateway:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(settings.data_dir, 0o700)
        self.blobs = settings.cache / "blobs"
        self.work = settings.cache / "work"
        for path in (self.blobs, self.work, settings.shares):
            path.mkdir(parents=True, exist_ok=True)
        # Samba users traverse a tmpfs namespace to their mode-0700 share roots.
        os.chmod(settings.data_dir, 0o711)
        os.chmod(settings.shares, 0o711)
        os.chmod(settings.cache, 0o700)
        self.db = Database()
        self.lock = self.db.lock
        if settings.postgres_dsn and settings.postgres_dsn.get_secret_value() and not settings.secret_key:
            self.db.close()
            raise ValueError("使用 PostgreSQL 时请设置 CAMMON_SECRET_KEY，以便重启后解密存储与摄像机凭据")
        self.secrets = Secrets(settings.secret_key.get_secret_value() if settings.secret_key else None)
        settings.secret_key = SecretStr(self.secrets.key.decode())
        self.handles: dict[int, OpenFile] = {}
        self.stopping = threading.Event()
        self.backend: Backend | None = None
        self.protocol_error: str | None = None
        self.worker_error: str | None = None
        self.cache_required = 0
        self._handle_counter = secrets.randbits(40)
        self.samba = None
        self.metadata = MetadataSynchronizer(self.db, settings)
        try:
            self.metadata.bootstrap()
        except BaseException:
            self.metadata.stop()
            self.db.close()
            raise
        self._configure_admin()
        stored = self.get_setting("storage")
        if stored:
            try:
                self.backend = make_backend(self._decode_storage(stored), settings.network_timeout_seconds)
            except Exception as exc:
                self.worker_error = f"存储初始化失败: {exc}"
        self.recover()

    def _configure_admin(self):
        if not self.get_setting("admin"):
            if len(self.settings.admin_password) < 12:
                raise ValueError("CAMMON_ADMIN_PASSWORD must contain at least 12 characters")
            self.set_setting("admin", {
                "username": self.settings.admin_username,
                "password_hash": password_hash(self.settings.admin_password),
            })

    def get_setting(self, key: str):
        row = self.db.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else None

    def set_setting(self, key: str, value):
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))

    def _decode_storage(self, config: dict) -> dict:
        result = dict(config)
        result["password"] = self.secrets.decrypt(result.pop("password_encrypted", "")) if result.get(
            "password_encrypted"
        ) else ""
        return result

    def storage_config(self, reveal: bool = False) -> dict | None:
        config = self.get_setting("storage")
        if not config:
            return None
        if reveal:
            return self._decode_storage(config)
        result = {k: v for k, v in config.items() if k != "password_encrypted"}
        result["has_password"] = bool(config.get("password_encrypted"))
        return result

    def configure_storage(self, config: dict):
        config = dict(config)
        previous = self.storage_config(reveal=True)
        if config.get("password") is None:
            config["password"] = previous.get("password", "") if previous else ""
        shares = [d["share"] for d in self.devices()]
        reject_self_storage(config, shares)
        with self.lock:
            if previous and any(config.get(k) != previous.get(k) for k in ("protocol", "address", "nfs_version")):
                if self.db.one("SELECT id FROM recordings WHERE state!='deleted' LIMIT 1"):
                    raise ValueError("已有录像时不能更换真实存储地址；请先完成手工迁移")
            backend = make_backend(config, self.settings.network_timeout_seconds)
            # Credential changes are safe; every in-flight task retains its own backend instance.
            saved = {k: v for k, v in config.items() if k != "password"}
            saved["password_encrypted"] = self.secrets.encrypt(config["password"]) if config["password"] else ""
            self.set_setting("storage", saved)
            self.backend = backend
        return self.storage_config()

    def devices(self) -> list[dict]:
        return self.db.all("SELECT * FROM devices ORDER BY created_at")

    def device(self, device_id: str, enabled: bool = True) -> dict:
        device = self.db.one("SELECT * FROM devices WHERE id=?", (device_id,))
        if not device or (enabled and not device["enabled"]):
            raise OSError(errno.EACCES, "Device unavailable")
        return device

    def create_device(self, name: str, retention_days: int = 30) -> dict:
        device_id = uuid.uuid4().hex
        username = "cam_" + device_id[:12]
        password = secrets.token_urlsafe(18)
        uid, gid = os.getuid(), os.getgid()
        if self.samba:
            uid, gid = self.samba.create_user(username, password)
        now = time.time()
        device = dict(id=device_id, name=name, share=username, username=username, uid=uid, gid=gid,
                      retention_days=retention_days, enabled=1, created_at=now)
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute(
                    "INSERT INTO devices VALUES (:id,:name,:share,:username,:uid,:gid,:retention_days,:enabled,:created_at)",
                    device,
                )
                self.set_setting("credential:" + device_id, self.secrets.encrypt(password))
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self._mirror_directory(device, "")
            if self.samba:
                self.samba.render()
        return {**device, "password": password}

    def update_device(self, device_id: str, values: dict) -> dict:
        close_tokens = []
        with self.lock:
            self.device(device_id, enabled=False)
            for key in ("name", "retention_days", "enabled"):
                if key in values:
                    self.db.execute(f"UPDATE devices SET {key}=? WHERE id=?", (values[key], device_id))
            if values.get("enabled") is False:
                close_tokens = [token for token, handle in self.handles.items()
                                if self.recording(handle.recording_id)["device_id"] == device_id]
            if self.samba:
                self.samba.render()
            device = self.device(device_id, enabled=False)
        for token in close_tokens:
            self.close(token)
        return device

    def reset_device_password(self, device_id: str) -> str:
        device = self.device(device_id, enabled=False)
        if not self.samba:
            raise ValueError("Samba is not enabled")
        password = secrets.token_urlsafe(18)
        self.samba.set_password(device["username"], password)
        self.set_setting("credential:" + device_id, self.secrets.encrypt(password))
        return password

    def device_password(self, device_id: str) -> str | None:
        encrypted = self.get_setting("credential:" + device_id)
        return self.secrets.decrypt(encrypted) if encrypted else None

    def blob(self, recording_id: int) -> Path:
        return self.blobs / str(recording_id)

    def _projection(self, device: dict, path: str) -> Path:
        return self.settings.shares / device["id"] / path

    def _mirror_directory(self, device: dict, path: str):
        target = self._projection(device, path)
        target.mkdir(parents=True, exist_ok=True)
        os.chmod(target, 0o700)
        if os.geteuid() == 0:
            os.chown(target, device["uid"], device["gid"])

    def _mirror_file(self, device: dict, path: str):
        target = self._projection(device, path)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
        if os.geteuid() == 0:
            os.chown(target, device["uid"], device["gid"])

    def mkdir(self, device_id: str, path: str):
        path = directory_path(path)
        with self.lock:
            device = self.device(device_id)
            if self.db.one("SELECT id FROM directories WHERE device_id=? AND path=?", (device_id, path)):
                raise FileExistsError(path)
            parent = path.rpartition("/")[0]
            if not stat.S_ISDIR(self.stat(device_id, parent)["mode"]):
                raise NotADirectoryError(parent)
            self.db.execute("INSERT INTO directories(device_id,path,modified_at) VALUES (?,?,?)",
                            (device_id, path, time.time()))
            self._mirror_directory(device, path)

    def recording(self, recording_id: int) -> dict:
        row = self.db.one("SELECT * FROM recordings WHERE id=? AND state!='deleted'", (recording_id,))
        if not row:
            raise FileNotFoundError(recording_id)
        return row

    def by_path(self, device_id: str, path: str) -> dict | None:
        return self.db.one(
            "SELECT * FROM recordings WHERE device_id=? AND path=? AND kind='original' AND state!='deleted'",
            (device_id, path),
        )

    def stat(self, device_id: str, path: str) -> dict:
        path = "/".join(path_parts(path))
        with self.lock:
            device = self.device(device_id)
            if not path:
                return dict(mode=stat.S_IFDIR | 0o700, size=0, inode=1,
                            mtime=device["created_at"], uid=device["uid"], gid=device["gid"])
            directory = self.db.one("SELECT * FROM directories WHERE device_id=? AND path=?", (device_id, path))
            if directory:
                return dict(mode=stat.S_IFDIR | 0o700, size=0, inode=directory["id"] * 2 + 2,
                            mtime=directory["modified_at"], uid=device["uid"], gid=device["gid"])
            row = self.by_path(device_id, path)
            if not row:
                raise FileNotFoundError(path)
            return dict(mode=stat.S_IFREG | (0o600 if row["state"] == "cached" else 0o400),
                        size=row["size"], inode=row["id"] * 2 + 3,
                        mtime=row["last_write"], uid=device["uid"], gid=device["gid"])

    def listdir(self, device_id: str, path: str = "", limit: int | None = None, offset: int = 0) -> list[str]:
        path = directory_path(path, allow_root=True)
        self.stat(device_id, path)
        prefix = path + "/" if path else ""
        length = len(prefix)
        bounds = (device_id, prefix, prefix + "\U0010ffff")
        rows = self.db.all(
            "SELECT substr(path,?+1) AS name FROM ("
            "SELECT path FROM directories WHERE device_id=? AND path>=? AND path<? UNION ALL "
            "SELECT path FROM recordings WHERE device_id=? AND path>=? AND path<? "
            "AND kind='original' AND state!='deleted') WHERE length(path)>? "
            "AND instr(substr(path,?+1),'/')=0 ORDER BY path LIMIT ? OFFSET ?",
            (length, *bounds, *bounds, length, length, limit if limit is not None else -1, offset),
        )
        return [row["name"] for row in rows]

    def cache_usage(self) -> int:
        return sum(p.stat().st_size for root in (self.blobs, self.work) for p in root.rglob("*") if p.is_file())

    def available(self) -> int:
        free = max(0, shutil.disk_usage(self.settings.cache).free - self.settings.cache_reserve_bytes)
        if self.settings.cache_limit_bytes:
            free = min(free, max(0, self.settings.cache_limit_bytes - self.cache_usage()))
        return free

    def reserve(self, count: int):
        if count > self.available():
            self.cache_required = count
            raise OSError(errno.ENOSPC, "本地缓存空间不足，已有录像已保留")

    def open(self, device_id: str, path: str, flags: int, session: str = "internal") -> int:
        with self.lock:
            device = self.device(device_id)
            normalized = "/".join(path_parts(path))
            try:
                metadata = self.stat(device_id, normalized)
                is_directory = stat.S_ISDIR(metadata["mode"])
            except FileNotFoundError:
                is_directory = False
            if is_directory or flags & getattr(os, "O_PATH", 0):
                self.stat(device_id, normalized)
                if flags & (os.O_TRUNC | os.O_CREAT) and not is_directory:
                    raise OSError(errno.EACCES, "Cannot mutate a path-reference handle")
                return 0
            path, channel, start, end, day = recording_path(normalized)
            row = self.by_path(device_id, path)
            writable = flags & os.O_ACCMODE != os.O_RDONLY
            if not row:
                if not flags & os.O_CREAT:
                    raise FileNotFoundError(path)
                if not writable:
                    raise OSError(errno.EACCES, "Creating a recording requires write access")
                parent = path.rpartition("/")[0]
                if not stat.S_ISDIR(self.stat(device_id, parent)["mode"]):
                    raise NotADirectoryError(parent)
                self.reserve(1)
                now = time.time()
                self.db.execute("BEGIN IMMEDIATE")
                try:
                    self.db.execute("UPDATE recordings SET successor=1 WHERE device_id=? AND channel=? "
                                    "AND kind='original' AND state='cached' AND start<?", (device_id, channel, start))
                    successor = bool(self.db.one("SELECT id FROM recordings WHERE device_id=? AND channel=? "
                                                 "AND kind='original' AND state!='deleted' AND start>? LIMIT 1",
                                                 (device_id, channel, start)))
                    cursor = self.db.execute(
                        "INSERT INTO recordings(device_id,path,channel,start,end,day,created_at,last_write,successor) "
                        "VALUES (?,?,?,?,?,?,?,?,?)", (device_id, path, channel, start, end, day, now, now, successor),
                    )
                    recording_id = cursor.lastrowid
                    self.blob(recording_id).touch(mode=0o600)
                    self._mirror_file(device, path)
                    self.db.execute("COMMIT")
                except Exception:
                    self.db.execute("ROLLBACK")
                    raise
                row = self.recording(recording_id)
            elif flags & os.O_CREAT and flags & os.O_EXCL:
                raise FileExistsError(path)
            if writable and row["state"] != "cached":
                raise OSError(errno.EACCES, "Completed recordings are read-only")
            token = self._new_handle(row, writable, session)
            if flags & os.O_TRUNC:
                if not writable:
                    self.close(token)
                    raise OSError(errno.EACCES, "Truncation requires write access")
                self.truncate(token, 0)
            return token

    def _new_handle(self, row: dict, writable: bool, session: str) -> int:
        if row["state"] == "deleting":
            raise FileNotFoundError(row["path"])
        fd = None
        if self.blob(row["id"]).exists():
            fd = os.open(self.blob(row["id"]), os.O_RDWR if writable else os.O_RDONLY)
        elif row["state"] != "stored":
            raise OSError(errno.EIO, "Recording cache is missing")
        self._handle_counter += 1
        token = self._handle_counter
        self.handles[token] = OpenFile(row["id"], writable, session, fd)
        return token

    def open_recording(self, recording_id: int, session: str = "http") -> int:
        with self.lock:
            return self._new_handle(self.recording(recording_id), False, session)

    def handle(self, token: int, writable: bool = False) -> OpenFile:
        handle = self.handles.get(token)
        if not handle or (writable and not handle.writable):
            raise OSError(errno.EBADF, "Invalid file handle")
        if writable and self.recording(handle.recording_id)["state"] != "cached":
            raise OSError(errno.EACCES, "Recording has been sealed")
        return handle

    def read(self, token: int, count: int, offset: int) -> bytes:
        if count < 0 or offset < 0 or count > 8 * 1024**2:
            raise OSError(errno.EINVAL, "Invalid read range")
        with self.lock:
            handle = self.handle(token)
            row = self.recording(handle.recording_id)
        with handle.lock:
            if handle.closed:
                raise OSError(errno.EBADF, "File handle is closed")
            if handle.fd is not None:
                return os.pread(handle.fd, count, offset)
            if not handle.remote:
                if not self.backend:
                    raise OSError(errno.EIO, "Storage is not configured")
                handle.remote = self.backend.reader(row["remote_path"])
            handle.remote.seek(offset)
            return handle.remote.read(count)

    def write(self, token: int, data: bytes, offset: int) -> int:
        if offset < 0:
            raise OSError(errno.EINVAL, "Invalid write offset")
        with self.lock:
            handle = self.handle(token, writable=True)
            row = self.recording(handle.recording_id)
            self.reserve(max(0, offset + len(data) - row["size"]))
            count = os.pwrite(handle.fd, data, offset)
            size = os.fstat(handle.fd).st_size
            self.db.execute("UPDATE recordings SET size=?,last_write=? WHERE id=?", (size, time.time(), row["id"]))
            return count

    def truncate(self, token: int, length: int):
        if length < 0:
            raise OSError(errno.EINVAL, "Invalid file length")
        with self.lock:
            handle = self.handle(token, writable=True)
            row = self.recording(handle.recording_id)
            self.reserve(max(0, length - row["size"]))
            os.ftruncate(handle.fd, length)
            self.db.execute("UPDATE recordings SET size=?,last_write=? WHERE id=?", (length, time.time(), row["id"]))

    def sync(self, token: int):
        with self.lock:
            handle = self.handle(token)
            if handle.fd is not None:
                os.fsync(handle.fd)

    def close(self, token: int):
        with self.lock:
            handle = self.handles.pop(token, None)
        if not handle:
            return
        with handle.lock:
            handle.closed = True
            if handle.fd is not None:
                if handle.writable:
                    os.fsync(handle.fd)
                os.close(handle.fd)
            if handle.remote:
                handle.remote.close()

    def close_session(self, session: str):
        with self.lock:
            tokens = [token for token, handle in self.handles.items() if handle.session == session]
        for token in tokens:
            self.close(token)

    def rename(self, device_id: str, old: str, new: str):
        old_path = "/".join(path_parts(old))
        with self.lock:
            device = self.device(device_id)
            if self.db.one("SELECT id FROM directories WHERE device_id=? AND path=?", (device_id, old_path)):
                return self._rename_directory(device, old_path, directory_path(new))
            new_path, channel, start, end, day = recording_path(new)
            row = self.by_path(device_id, old_path)
            if not row or row["state"] != "cached":
                raise OSError(errno.EACCES, "Only cached recordings may be renamed")
            if self.by_path(device_id, new_path):
                raise FileExistsError(new_path)
            self.stat(device_id, new_path.rpartition("/")[0])
            self._projection(device, old_path).rename(self._projection(device, new_path))
            successor = bool(self.db.one("SELECT id FROM recordings WHERE device_id=? AND channel=? AND id!=? "
                                         "AND kind='original' AND state!='deleted' AND start>? LIMIT 1",
                                         (device_id, channel, row["id"], start)))
            if channel == row["channel"]:
                successor = successor or bool(row["successor"])
            self.db.execute("UPDATE recordings SET path=?,channel=?,start=?,end=?,day=?,last_write=?,successor=? WHERE id=?",
                            (new_path, channel, start, end, day, time.time(), successor, row["id"]))
            self.db.execute("UPDATE recordings SET successor=1 WHERE device_id=? AND channel=? AND id!=? "
                            "AND kind='original' AND state='cached' AND start<?", (device_id, channel, row["id"], start))

    def _rename_directory(self, device: dict, old: str, new: str):
        if new == old:
            return
        if new.startswith(old + "/"):
            raise OSError(errno.EINVAL, "Cannot move a directory into itself")
        try:
            self.stat(device["id"], new)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(new)
        if not stat.S_ISDIR(self.stat(device["id"], new.rpartition("/")[0])["mode"]):
            raise NotADirectoryError(new)
        directories = [r for r in self.db.all("SELECT * FROM directories WHERE device_id=?", (device["id"],))
                       if r["path"] == old or r["path"].startswith(old + "/")]
        files = [r for r in self.db.all("SELECT * FROM recordings WHERE device_id=? AND kind='original' AND state!='deleted'",
                                      (device["id"],)) if r["path"].startswith(old + "/")]
        for row in directories:
            directory_path(new + row["path"][len(old):])
        for row in files:
            if row["state"] != "cached":
                raise OSError(errno.EACCES, "Completed recording paths cannot change")
            recording_path(new + row["path"][len(old):])
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self._projection(device, old).rename(self._projection(device, new))
            for row in directories:
                self.db.execute("UPDATE directories SET path=? WHERE id=?", (new + row["path"][len(old):], row["id"]))
            for row in files:
                self.db.execute("UPDATE recordings SET path=? WHERE id=?", (new + row["path"][len(old):], row["id"]))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def unlink(self, device_id: str, path: str, directory: bool = False):
        if not directory:
            path, *_ = recording_path(path)
            with self.lock:
                device = self.device(device_id)
                row = self.by_path(device_id, path)
                if not row:
                    raise FileNotFoundError(path)
                if row["state"] != "cached":
                    raise OSError(errno.EACCES, "Completed recordings may only be deleted by retention")
                if any(h.recording_id == row["id"] for h in self.handles.values()):
                    raise OSError(errno.EBUSY, "Close recording handles before deleting a cached file")
                self.db.execute("UPDATE recordings SET state='deleted' WHERE id=?", (row["id"],))
                self._projection(device, path).unlink(missing_ok=True)
                self.blob(row["id"]).unlink(missing_ok=True)
            return
        path = directory_path(path)
        with self.lock:
            device = self.device(device_id)
            if self.listdir(device_id, path):
                raise OSError(errno.ENOTEMPTY, "Directory is not empty")
            self._projection(device, path).rmdir()
            self.db.execute("DELETE FROM directories WHERE device_id=? AND path=?", (device_id, path))

    def seal_candidates(self, now: float | None = None) -> list[int]:
        now = now or time.time()
        with self.lock:
            busy = {h.recording_id for h in self.handles.values() if h.writable}
            sealed = []
            for row in self.db.all("SELECT * FROM recordings WHERE state='cached' AND kind='original'"):
                wait = self.settings.successor_quiet_seconds if row["successor"] else self.settings.idle_seal_seconds
                if row["id"] not in busy and now - row["last_write"] >= wait:
                    if not self.blob(row["id"]).exists():
                        self.db.execute("UPDATE recordings SET error=? WHERE id=?", ("缓存文件缺失，需检查缓存挂载", row["id"]))
                        continue
                    # The same lock guards new write handles, so sealing cannot race a camera reopen.
                    with self.blob(row["id"]).open("rb") as stream:
                        os.fsync(stream.fileno())
                    self.db.execute("UPDATE recordings SET state='pending' WHERE id=?", (row["id"],))
                    sealed.append(row["id"])
            return sealed

    def publish(self, source: Path, remote: str, backend: Backend) -> tuple[int, str]:
        backend.stopping = self.stopping
        backend.check_cancelled()
        size, digest = source.stat().st_size, file_hash(source, self.stopping)
        if backend.exists(remote):
            if backend.stat(remote) == size and backend.sha256(remote) == digest:
                return size, digest
            raise OSError("Remote final path already contains different content")
        temporary = remote + ".cammon-part"
        backend.upload(temporary, source)
        if backend.stat(temporary) != size or backend.sha256(temporary) != digest:
            raise OSError("Remote upload verification failed")
        backend.rename(temporary, remote)
        return size, digest

    def flush_one(self, recording_id: int) -> bool:
        with self.lock:
            row = self.recording(recording_id)
            backend = self.backend
            if row["state"] not in ("pending", "uploading") or not backend:
                return False
            remote = row["remote_path"] or f"originals/{row['device_id']}/{row['path']}"
            self.db.execute("UPDATE recordings SET state='uploading',remote_path=? WHERE id=?", (remote, row["id"]))
        try:
            size, digest = self.publish(self.blob(row["id"]), remote, backend)
            with self.lock:
                self.db.execute("UPDATE recordings SET state='stored',size=?,sha256=?,error=NULL,retry_at=0 WHERE id=?",
                                (size, digest, row["id"]))
                self.blob(row["id"]).unlink(missing_ok=True)
            return True
        except Exception as exc:
            self.db.execute("UPDATE recordings SET state='pending',error=?,retry_at=? WHERE id=?",
                            (str(exc), time.time() + 30, row["id"]))
            return False

    def recover(self):
        with self.lock:
            # Temporary recordings have no recovery guarantee. Discard files from
            # prior processes, including orphans whose metadata never reached PG.
            for directory in (self.blobs, self.work):
                for child in directory.iterdir():
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
            self.db.execute("UPDATE recordings SET state='deleted',error='进程重启后临时录像已丢弃' "
                            "WHERE state IN ('cached','pending','uploading')")
            self.db.execute("UPDATE recordings SET state='stored' WHERE state='deleting'")
            self.db.execute("UPDATE archive_jobs SET status='queued' WHERE status='running'")
            for device in self.devices():
                self._mirror_directory(device, "")
                for row in self.db.all("SELECT path FROM directories WHERE device_id=? ORDER BY length(path)", (device["id"],)):
                    self._mirror_directory(device, row["path"])
                for row in self.db.all("SELECT * FROM recordings WHERE device_id=? AND kind='original' AND state!='deleted'",
                                       (device["id"],)):
                    self._mirror_file(device, row["path"])

    def query_files(self, filters: dict, limit: int = 100, offset: int = 0) -> dict:
        where, args = self._filters(filters)
        totals = self.db.one(f"SELECT count(*) AS count,coalesce(sum(size),0) AS bytes FROM recordings r WHERE {where}", args)
        rows = self.db.all(f"SELECT r.*,d.name AS device_name FROM recordings r JOIN devices d ON d.id=r.device_id "
                           f"WHERE {where} ORDER BY start DESC,r.id DESC LIMIT ? OFFSET ?", (*args, limit, offset))
        return {"items": rows, **totals}

    def statistics(self, filters: dict) -> dict:
        where, args = self._filters(filters)
        total = self.db.one(f"SELECT count(*) AS count,coalesce(sum(size),0) AS bytes FROM recordings r WHERE {where}", args)
        groups = self.db.all(f"SELECT day,kind,count(*) AS count,sum(size) AS bytes FROM recordings r WHERE {where} "
                             "GROUP BY day,kind ORDER BY day", args)
        return {**total, "groups": groups}

    @staticmethod
    def _filters(filters: dict) -> tuple[str, list]:
        parts, args = ["r.state!='deleted'"], []
        for key in ("device_id", "channel", "kind", "day"):
            if filters.get(key) is not None:
                parts.append(f"r.{key}=?")
                args.append(filters[key])
        if filters.get("year") is not None:
            parts.append("substr(r.day,1,4)=?")
            args.append(f"{filters['year']:04d}")
        if filters.get("month") is not None:
            parts.append("substr(r.day,6,2)=?")
            args.append(f"{filters['month']:02d}")
        return " AND ".join(parts), args

    def status(self) -> dict:
        usage, free = self.cache_usage(), self.available()
        if free >= self.cache_required:
            self.cache_required = 0
        return {
            "storage_configured": self.backend is not None, "cache_bytes": usage, "cache_available_bytes": free,
            "cache_limit_bytes": self.settings.cache_limit_bytes,
            "cache_error": "本地缓存空间不足，已有录像已保留；请恢复后端连接或扩容缓存" if free == 0 or self.cache_required else None,
            "pending_files": self.db.one("SELECT count(*) AS n FROM recordings WHERE state IN ('pending','uploading')")["n"],
            "writing_handles": sum(h.writable for h in self.handles.values()),
            "protocol_error": self.protocol_error, "worker_error": self.worker_error,
            "alerts": self.db.all("SELECT id,path,error FROM recordings WHERE error IS NOT NULL AND state!='deleted'"),
            "jobs": self.db.all("SELECT * FROM archive_jobs ORDER BY created_at DESC LIMIT 30"),
            "timezone": "Asia/Shanghai",
            "metadata": self.metadata.status(),
        }

    def shutdown(self):
        self.metadata.stop()
        for token in list(self.handles):
            self.close(token)
        self.db.close()
