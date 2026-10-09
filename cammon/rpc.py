"""VFS transport: two uint32 big-endian lengths, JSON metadata, then raw bytes."""

import errno
import json
import os
import socket
import socketserver
import struct
import threading
import uuid

MAX_FRAME = 8 * 1024**2


def receive(sock: socket.socket, count: int) -> bytes:
    output = bytearray()
    while len(output) < count:
        chunk = sock.recv(count - len(output))
        if not chunk:
            raise EOFError
        output.extend(chunk)
    return bytes(output)


def send_frame(sock, metadata, data=b""):
    header = json.dumps(metadata, separators=(",", ":"), ensure_ascii=False).encode()
    sock.sendall(struct.pack("!II", len(header), len(data)) + header + data)


def read_frame(sock):
    head_size, body_size = struct.unpack("!II", receive(sock, 8))
    if head_size > 65536 or body_size > MAX_FRAME:
        raise ValueError("Frame too large")
    metadata = json.loads(receive(sock, head_size))
    if not isinstance(metadata, dict):
        raise ValueError("Expected an object")
    return metadata, receive(sock, body_size)


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        gateway = self.server.gateway
        with self.server.active_lock:
            self.server.active.add(self.request)
        session = uuid.uuid4().hex
        device_id = None
        _, peer_uid, _ = struct.unpack("3i", self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        try:
            while True:
                request, body = read_frame(self.request)
                try:
                    op = request["op"]
                    result, data = None, b""
                    if op == "hello":
                        if device_id:
                            raise OSError(errno.EACCES, "Session already bound")
                        device = gateway.device(request["device"])
                        if peer_uid not in (0, os.geteuid(), device["uid"]):
                            raise OSError(errno.EACCES, "Account does not own this device")
                        device_id = device["id"]
                        result = {"version": 1}
                    else:
                        if not device_id:
                            raise OSError(errno.EACCES, "Session is not authenticated")
                        gateway.device(device_id)
                        token = request.get("handle")
                        if token:
                            handle = gateway.handle(int(token))
                            if handle.session != session:
                                raise OSError(errno.EACCES, "Handle belongs to another session")
                        if op == "stat":
                            result = gateway.stat(device_id, request["path"])
                        elif op == "list":
                            result = gateway.listdir(device_id, request["path"], limit=128,
                                                     offset=max(0, int(request.get("offset", 0))))
                        elif op == "fstat":
                            row = gateway.recording(gateway.handle(token).recording_id)
                            result = gateway.stat(device_id, row["path"])
                        elif op == "open":
                            result = gateway.open(device_id, request["path"], int(request["flags"]), session)
                        elif op == "read":
                            data = gateway.read(token, int(request["count"]), int(request["offset"]))
                            result = len(data)
                        elif op == "write":
                            result = gateway.write(token, body, int(request["offset"]))
                        elif op == "truncate":
                            gateway.truncate(token, int(request["length"]))
                        elif op == "sync":
                            gateway.sync(token)
                        elif op == "close":
                            gateway.close(token)
                        elif op == "mkdir":
                            gateway.mkdir(device_id, request["path"])
                        elif op == "rename":
                            gateway.rename(device_id, request["path"], request["destination"])
                        elif op == "unlink":
                            gateway.unlink(device_id, request["path"], bool(request.get("directory")))
                        elif op == "capacity":
                            result = {"free": gateway.available(), "total": gateway.settings.cache_limit_bytes or
                                      __import__("shutil").disk_usage(gateway.settings.cache).total}
                        else:
                            raise OSError(errno.ENOSYS, "Unsupported VFS operation")
                    send_frame(self.request, {"ok": True, "result": result}, data)
                except Exception as exc:
                    send_frame(self.request, {"ok": False, "errno": getattr(exc, "errno", None) or errno.EIO,
                                              "error": str(exc)})
        except (EOFError, OSError, ValueError):
            pass
        finally:
            gateway.close_session(session)
            with self.server.active_lock:
                self.server.active.discard(self.request)


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = False

    def __init__(self, gateway):
        path = gateway.settings.socket_path
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o755)
        if path.exists():
            # A live socket is an ownership conflict, never unlink another running gateway.
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                try:
                    probe.connect(str(path))
                except (ConnectionRefusedError, FileNotFoundError):
                    path.unlink(missing_ok=True)
                else:
                    raise RuntimeError(f"VFS service is already running at {path}")
        self.gateway = gateway
        self.active = set()
        self.active_lock = threading.Lock()
        super().__init__(str(path), Handler)
        os.chmod(path, 0o660)
        if gateway.samba:
            os.chown(path, os.geteuid(), gateway.samba.group_id)
        self.thread = threading.Thread(target=self.serve_forever, name="vfs-rpc", daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.shutdown()
        with self.active_lock:
            for connection in self.active:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        self.server_close()
        self.gateway.settings.socket_path.unlink(missing_ok=True)
