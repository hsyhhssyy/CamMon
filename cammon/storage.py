"""User-space storage adapters. No kernel mounts or FUSE are used."""

import errno
import hashlib
import os
import shutil
import socket
import uuid
from abc import ABC, abstractmethod
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

CHUNK = 1024 * 1024


def safe_key(path: str) -> str:
    parts = PurePosixPath(path).parts
    if not parts or path.startswith("/") or any(p in ("..", ".") for p in parts) or "\\" in path:
        raise ValueError("Invalid storage key")
    return "/".join(parts)


class Backend(ABC):
    def check_cancelled(self):
        event = getattr(self, "stopping", None)
        if event is not None and event.is_set():
            raise InterruptedError("服务正在停止，任务将在重启后恢复")

    @abstractmethod
    def reader(self, path: str): ...

    @abstractmethod
    def upload(self, path: str, source: Path): ...

    @abstractmethod
    def mkdirs(self, path: str): ...

    @abstractmethod
    def stat(self, path: str) -> int: ...

    @abstractmethod
    def rename(self, source: str, destination: str): ...

    @abstractmethod
    def remove(self, path: str): ...

    def exists(self, path: str) -> bool:
        try:
            self.stat(path)
            return True
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return False
            raise

    def sha256(self, path: str) -> str:
        digest = hashlib.sha256()
        with self.reader(path) as stream:
            while data := stream.read(CHUNK):
                self.check_cancelled()
                digest.update(data)
        return digest.hexdigest()

    def probe(self, local_dir: Path) -> dict:
        import uuid

        name = f".cammon-probe-{uuid.uuid4().hex}"
        source = local_dir / name
        source.write_bytes(os.urandom(4096))
        target = name + ".renamed"
        try:
            self.upload(name, source)
            self.rename(name, target)
            if self.stat(target) != source.stat().st_size or self.sha256(target) != file_hash(source):
                raise OSError("Storage verification failed")
            self.remove(target)
            return {"ok": True, "message": "连接、写入、读取、改名和删除均成功"}
        finally:
            source.unlink(missing_ok=True)
            for key in (name, target):
                try:
                    self.remove(key)
                except OSError:
                    pass


def file_hash(path: Path, stopping=None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while data := stream.read(CHUNK):
            if stopping is not None and stopping.is_set():
                raise InterruptedError("服务正在停止")
            digest.update(data)
    return digest.hexdigest()


class LocalBackend(Backend):
    """Test fixture only; intentionally unavailable in the public storage configuration."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def _path(self, path: str) -> Path:
        return self.root / safe_key(path)

    def reader(self, path: str):
        return self._path(path).open("rb")

    def mkdirs(self, path: str):
        if path and path != ".":
            self._path(path).mkdir(parents=True, exist_ok=True)

    def upload(self, path: str, source: Path):
        self.mkdirs(str(PurePosixPath(path).parent))
        with source.open("rb") as src, self._path(path).open("wb") as dst:
            shutil.copyfileobj(src, dst, CHUNK)
            dst.flush()
            os.fsync(dst.fileno())

    def stat(self, path: str) -> int:
        return self._path(path).stat().st_size

    def rename(self, source: str, destination: str):
        if self.exists(destination):
            raise FileExistsError(destination)
        self._path(source).rename(self._path(destination))
        fd = os.open(self._path(destination).parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def remove(self, path: str):
        self._path(path).unlink()


class SMBBackend(Backend):
    def __init__(self, config: dict, timeout: int = 30):
        import smbclient

        self.client = smbclient
        url = urlsplit(config["address"])
        segments = [unquote(x) for x in url.path.split("/") if x]
        if url.scheme != "smb" or not url.hostname or not segments or url.username or url.password:
            raise ValueError("SMB 地址格式为 smb://主机/共享/可选子目录，凭据请单独填写")
        if any(p in (".", "..") or "\\" in p for p in segments):
            raise ValueError("Invalid SMB path")
        self.host = url.hostname
        self.root = "\\\\" + self.host + "\\" + "\\".join(segments)
        username = config.get("username", "")
        if config.get("domain"):
            username = config["domain"] + "\\" + username
        self.options = dict(
            username=username, password=config.get("password", ""), port=url.port or 445,
            connection_timeout=timeout, auth_protocol="ntlm", encrypt=config.get("encrypt", False),
        )

    def _path(self, path: str) -> str:
        return self.root + "\\" + safe_key(path).replace("/", "\\")

    def reader(self, path: str):
        return self.client.open_file(self._path(path), mode="rb", buffering=0, **self.options)

    def mkdirs(self, path: str):
        target = self.root if path in ("", ".") else self._path(path)
        self.client.makedirs(target, exist_ok=True, **self.options)

    def upload(self, path: str, source: Path):
        self.mkdirs(str(PurePosixPath(path).parent))
        with source.open("rb") as src, self.client.open_file(
            self._path(path), mode="wb", buffering=0, **self.options
        ) as dst:
            while data := src.read(CHUNK):
                self.check_cancelled()
                view = memoryview(data)
                while view:
                    count = dst.write(view)
                    if not count:
                        raise OSError("Short SMB write")
                    view = view[count:]
            dst.flush()

    def stat(self, path: str) -> int:
        return self.client.stat(self._path(path), **self.options).st_size

    def rename(self, source: str, destination: str):
        self.client.rename(self._path(source), self._path(destination), **self.options)

    def remove(self, path: str):
        try:
            self.client.remove(self._path(path), **self.options)
        except OSError as exc:
            raise OSError(exc.errno, str(exc)) from exc


class NFSReader:
    def __init__(self, backend, path: str):
        self.backend = backend
        self.lib, self.ffi = backend.lib, backend.ffi
        self.ctx = backend.connect()
        output = self.ffi.new("void **")
        try:
            backend.check(self.ctx, self.lib.cm_nfs_open(self.ctx, path.encode(), output))
        except Exception:
            self.lib.cm_nfs_destroy(self.ctx)
            self.ctx = None
            raise
        self.handle = output[0]
        self.offset = 0

    def read(self, count: int = CHUNK) -> bytes:
        count = CHUNK if count < 0 else count
        buf = self.ffi.new("char[]", count)
        size = self.backend.check(
            self.ctx, self.lib.cm_nfs_read(self.ctx, self.handle, self.offset, count, buf)
        )
        self.offset += size
        return bytes(self.ffi.buffer(buf, size))

    def seek(self, offset: int, whence: int = 0):
        if whence != 0 or offset < 0:
            raise ValueError("Only absolute seeks are supported")
        self.offset = offset
        return offset

    def close(self):
        if self.ctx:
            self.lib.cm_nfs_close(self.ctx, self.handle)
            self.lib.cm_nfs_destroy(self.ctx)
            self.ctx = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class NFSBackend(Backend):
    def __init__(self, config: dict, timeout: int = 30):
        from cammon._nfs import ffi, lib

        self.ffi, self.lib = ffi, lib
        self.url = urlsplit(config["address"])
        if self.url.scheme != "nfs" or not self.url.hostname or not self.url.path or self.url.username:
            raise ValueError("NFS 地址格式为 nfs://主机/导出目录，导出目录必须完整填写")
        self.export = unquote(self.url.path).rstrip("/") or "/"
        self.version = int(config.get("nfs_version", 3))
        if self.version not in (3, 4):
            raise ValueError("Supported NFS versions: 3 and 4.0")
        self.uid = int(config.get("uid", 65534))
        self.gid = int(config.get("gid", 65534))
        self.timeout = timeout

    def connect(self):
        client_id = ("cammon-" + uuid.uuid4().hex).encode()
        ctx = self.lib.cm_nfs_create(self.version, self.uid, self.gid, self.timeout, client_id)
        if ctx == self.ffi.NULL:
            raise OSError("Cannot create NFS context")
        try:
            address = f"nfs://{self.url.netloc}{self.export.rstrip('/')}/"
            self.check(ctx, self.lib.cm_nfs_connect(ctx, address.encode()))
        except Exception:
            self.lib.cm_nfs_destroy(ctx)
            raise
        return ctx

    def check(self, ctx, result: int) -> int:
        if result < 0:
            message = self.ffi.string(self.lib.cm_nfs_error(ctx)).decode(errors="replace")
            raise OSError(-result, message)
        return result

    def reader(self, path: str):
        return NFSReader(self, "/" + safe_key(path))

    def mkdirs(self, path: str):
        if path in ("", "."):
            return
        ctx = self.connect()
        try:
            current = ""
            for part in safe_key(path).split("/"):
                current += "/" + part
                result = self.lib.cm_nfs_mkdir(ctx, current.encode())
                if result != -17:  # EEXIST
                    self.check(ctx, result)
        finally:
            self.lib.cm_nfs_destroy(ctx)

    def upload(self, path: str, source: Path):
        self.mkdirs(str(PurePosixPath(path).parent))
        ctx = self.connect()
        output = self.ffi.new("void **")
        handle = None
        try:
            self.check(ctx, self.lib.cm_nfs_create_file(ctx, ("/" + safe_key(path)).encode(), output))
            handle = output[0]
            offset = 0
            with source.open("rb") as stream:
                while data := stream.read(CHUNK):
                    self.check_cancelled()
                    while data:
                        count = self.check(ctx, self.lib.cm_nfs_write(ctx, handle, offset, len(data), data))
                        if not count:
                            raise OSError("Short NFS write")
                        offset += count
                        data = data[count:]
            self.check(ctx, self.lib.cm_nfs_sync(ctx, handle))
            self.check(ctx, self.lib.cm_nfs_close(ctx, handle))
            handle = None
        finally:
            if handle is not None:
                self.lib.cm_nfs_close(ctx, handle)
            self.lib.cm_nfs_destroy(ctx)

    def stat(self, path: str) -> int:
        ctx = self.connect()
        size = self.ffi.new("uint64_t *")
        try:
            self.check(ctx, self.lib.cm_nfs_size(ctx, ("/" + safe_key(path)).encode(), size))
            return int(size[0])
        finally:
            self.lib.cm_nfs_destroy(ctx)

    def rename(self, source: str, destination: str):
        if self.exists(destination):
            raise FileExistsError(destination)
        self._operation("cm_nfs_rename", source, destination)

    def remove(self, path: str):
        self._operation("cm_nfs_unlink", path)

    def _operation(self, name: str, *paths: str):
        ctx = self.connect()
        try:
            self.check(ctx, getattr(self.lib, name)(ctx, *[("/" + safe_key(p)).encode() for p in paths]))
        finally:
            self.lib.cm_nfs_destroy(ctx)


def make_backend(config: dict, timeout: int = 30) -> Backend:
    if config["protocol"] == "smb":
        return SMBBackend(config, timeout)
    if config["protocol"] == "nfs":
        return NFSBackend(config, timeout)
    raise ValueError("Only SMB2/3 and NFS storage are supported")


def reject_self_storage(config: dict, gateway_shares: list[str]):
    if config["protocol"] != "smb":
        return
    url = urlsplit(config["address"])
    share = unquote(url.path).strip("/").split("/")[0].casefold()
    if share not in {s.casefold() for s in gateway_shares} or (url.port or 445) != 445:
        return
    addresses = {"127.0.0.1", "::1"}
    for host in (socket.gethostname(), url.hostname):
        try:
            resolved = {r[4][0] for r in socket.getaddrinfo(host, None)}
        except OSError:
            continue
        if host == url.hostname and resolved & addresses:
            raise ValueError("真实存储不能指向 CamMon 自身的共享目录")
        addresses |= resolved
