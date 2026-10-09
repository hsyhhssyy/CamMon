import hashlib
import logging
import mimetypes
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from cammon.archive import Worker
from cammon.config import Settings
from cammon.core import Gateway
from cammon.rpc import Server
from cammon.samba import SambaManager
from cammon.security import password_matches
from cammon.storage import CHUNK, make_backend, reject_self_storage

log = logging.getLogger(__name__)


class Login(BaseModel):
    username: str
    password: str


class DeviceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    retention_days: int = Field(default=30, ge=1, le=36500)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value):
        if not value.strip():
            raise ValueError("设备名称不能为空")
        return value.strip()


class DeviceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    retention_days: int | None = Field(default=None, ge=1, le=36500)
    enabled: bool | None = None

    @field_validator("name")
    @classmethod
    def valid_name(cls, value):
        return DeviceCreate.valid_name(value) if value is not None else None


class StorageConfig(BaseModel):
    protocol: Literal["smb", "nfs"]
    address: str = Field(min_length=1, max_length=2000)
    username: str = Field(default="", max_length=200)
    password: str | None = Field(default=None, max_length=2000)
    domain: str = Field(default="", max_length=200)
    encrypt: bool = False
    nfs_version: Literal[3, 4] = 3
    uid: int = Field(default=65534, ge=0, le=2**31 - 1)
    gid: int = Field(default=65534, ge=0, le=2**31 - 1)

    @field_validator("address")
    @classmethod
    def clean_address(cls, value):
        if any(c in value for c in ("\n", "\r", "\x00")):
            raise ValueError("Invalid storage address")
        return value.strip()


def filters(
    device_id: str | None = None, channel: str | None = None,
    kind: Literal["original", "archive"] | None = None, day: date | None = None,
    year: Annotated[int | None, Query(ge=1, le=9999)] = None,
    month: Annotated[int | None, Query(ge=1, le=12)] = None,
):
    return dict(device_id=device_id, channel=channel, kind=kind, day=day.isoformat() if day else None,
                year=year, month=month)


def parse_range(header: str | None, size: int) -> tuple[int, int, bool]:
    if not header:
        return 0, max(0, size - 1), False
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header)
    if not match or not any(match.groups()) or size == 0:
        raise HTTPException(416, "Invalid byte range", headers={"Content-Range": f"bytes */{size}"})
    first, last = match.groups()
    if first:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
    else:
        suffix = int(last)
        if suffix == 0:
            raise HTTPException(416, "Invalid suffix range", headers={"Content-Range": f"bytes */{size}"})
        start, end = max(0, size - suffix), size - 1
    if start >= size or start > end:
        raise HTTPException(416, "Unsatisfiable range", headers={"Content-Range": f"bytes */{size}"})
    return start, end, True


def create_app(settings: Settings | None = None, gateway: Gateway | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        owns_gateway = gateway is None
        g = gateway or Gateway(settings)
        app.state.gateway = g
        samba = None
        rpc = None
        worker = None
        try:
            g.metadata.start()
            if settings.samba_enabled:
                samba = SambaManager(g)
                g.samba = samba
                samba.prepare()
            rpc = Server(g)
            rpc.start()
            if samba:
                try:
                    samba.start()
                except Exception as exc:
                    g.protocol_error = str(exc)
                    log.exception("SMB service startup failed")
            if settings.worker_enabled:
                worker = Worker(g)
                worker.start()
            yield
        finally:
            g.metadata.stop()
            if samba:
                samba.stop()
            if rpc:
                rpc.stop()
            if worker:
                worker.stop()
            if owns_gateway:
                g.shutdown()

    app = FastAPI(title="CamMon", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def browser_protection(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/"):
            if request.headers.get("x-cammon-request") != "1":
                return JSONResponse({"detail": "Missing request header"}, 403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "no-cache"
        return response

    def get_gateway(request: Request) -> Gateway:
        return request.app.state.gateway

    def authenticated(request: Request, g: Gateway = Depends(get_gateway)):
        token = request.cookies.get("cammon_session", "")
        digest = hashlib.sha256(token.encode()).hexdigest()
        if not token or not g.db.one("SELECT * FROM sessions WHERE token_hash=? AND expires>?", (digest, time.time())):
            raise HTTPException(401, "请先登录")
        return g

    auth = Depends(authenticated)

    @app.exception_handler(OSError)
    async def os_error(request, exc):
        status = 404 if isinstance(exc, FileNotFoundError) else 409
        return JSONResponse({"detail": str(exc)}, status)

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"detail": str(exc)}, 422)

    @app.post("/api/auth/login")
    def login(data: Login, response: Response, g: Gateway = Depends(get_gateway)):
        admin = g.get_setting("admin")
        if data.username != admin["username"] or not password_matches(data.password, admin["password_hash"]):
            raise HTTPException(401, "用户名或密码错误")
        token = secrets.token_urlsafe(32)
        g.db.execute("DELETE FROM sessions WHERE expires<=?", (time.time(),))
        g.db.execute("INSERT INTO sessions VALUES (?,?)", (hashlib.sha256(token.encode()).hexdigest(), time.time() + 43200))
        response.set_cookie("cammon_session", token, httponly=True, secure=settings.cookie_secure,
                            samesite="strict", max_age=43200)
        return {"username": admin["username"]}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response, g: Gateway = auth):
        token = request.cookies.get("cammon_session", "")
        g.db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
        response.delete_cookie("cammon_session")
        return {"ok": True}

    @app.get("/api/auth/me")
    def me(g: Gateway = auth):
        return {"username": g.get_setting("admin")["username"]}

    @app.get("/healthz")
    def health(g: Gateway = Depends(get_gateway)):
        error = g.protocol_error or (g.samba.health() if g.samba else None)
        return JSONResponse({"ok": not error, "protocol_error": error}, 503 if error else 200)

    @app.get("/api/status")
    def status(g: Gateway = auth):
        result = g.status()
        if g.samba:
            result["protocol_error"] = result["protocol_error"] or g.samba.health()
        return result

    @app.get("/api/devices")
    def devices(g: Gateway = auth):
        return g.devices()

    @app.post("/api/devices", status_code=201)
    def add_device(data: DeviceCreate, g: Gateway = auth):
        return g.create_device(data.name.strip(), data.retention_days)

    @app.patch("/api/devices/{device_id}")
    def edit_device(device_id: str, data: DeviceUpdate, g: Gateway = auth):
        return g.update_device(device_id, data.model_dump(exclude_none=True))

    @app.post("/api/devices/{device_id}/reset-password")
    def reset_password(device_id: str, g: Gateway = auth):
        return {"password": g.reset_device_password(device_id)}

    @app.get("/api/storage")
    def get_storage(g: Gateway = auth):
        return g.storage_config()

    @app.put("/api/storage")
    def put_storage(data: StorageConfig, g: Gateway = auth):
        return g.configure_storage(data.model_dump())

    @app.post("/api/storage/test")
    def test_storage(data: StorageConfig, g: Gateway = auth):
        config = data.model_dump()
        if config.get("password") is None:
            current = g.storage_config(reveal=True)
            config["password"] = current.get("password", "") if current else ""
        reject_self_storage(config, [d["share"] for d in g.devices()])
        try:
            return make_backend(config, settings.network_timeout_seconds).probe(g.work)
        except Exception as exc:
            raise HTTPException(409, f"存储测试失败: {exc}") from exc

    @app.get("/api/files")
    def files(query: dict = Depends(filters), limit: int = Query(default=100, ge=1, le=500),
              offset: int = Query(default=0, ge=0), g: Gateway = auth):
        return g.query_files(query, limit, offset)

    @app.get("/api/stats")
    def stats(query: dict = Depends(filters), g: Gateway = auth):
        return g.statistics(query)

    @app.api_route("/api/files/{recording_id}/download", methods=["GET", "HEAD"])
    def download(recording_id: int, request: Request, g: Gateway = auth):
        row = g.recording(recording_id)
        start, end, partial = parse_range(request.headers.get("range"), row["size"])
        length = end - start + 1 if row["size"] else 0
        headers = {"Accept-Ranges": "bytes", "Content-Length": str(length),
                   "Content-Disposition": "attachment; filename*=UTF-8''" + quote(Path(row["path"]).name)}
        if partial:
            headers["Content-Range"] = f"bytes {start}-{end}/{row['size']}"
        if request.method == "HEAD":
            return Response(status_code=206 if partial else 200, headers=headers, media_type="video/mp4")
        token = g.open_recording(recording_id)

        def stream():
            offset, remaining = start, length
            try:
                while remaining:
                    data = g.read(token, min(CHUNK, remaining), offset)
                    if not data:
                        raise OSError("Recording read ended before the advertised length")
                    yield data
                    offset += len(data)
                    remaining -= len(data)
            finally:
                g.close(token)

        return StreamingResponse(stream(), status_code=206 if partial else 200, headers=headers, media_type="video/mp4")

    @app.post("/api/jobs/{job_id}/retry")
    def retry_job(job_id: str, g: Gateway = auth):
        job = g.db.one("SELECT * FROM archive_jobs WHERE id=?", (job_id,))
        if not job or job["status"] != "failed":
            raise HTTPException(409, "只能重试失败的归档任务")
        g.db.execute("UPDATE archive_jobs SET status='queued',retry_at=0 WHERE id=?", (job_id,))
        return {"ok": True}

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str):
        root = settings.frontend_dir.resolve()
        candidate = (root / path).resolve()
        if path.startswith("api/") or not candidate.is_relative_to(root):
            raise HTTPException(404)
        if candidate.is_file():
            return FileResponse(candidate, media_type=mimetypes.guess_type(candidate)[0])
        index = root / "index.html"
        if index.exists():
            return FileResponse(index)
        return JSONResponse({"message": "CamMon API is running. Build the React frontend or run its dev server."})

    return app
