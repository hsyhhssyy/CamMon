from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CAMMON_", env_file=".env", extra="ignore")
    data_dir: Path = Path("/run/cammon/runtime")
    cache_dir: Path | None = None
    host: str = "0.0.0.0"
    port: int = 18080
    admin_username: str = "admin"
    admin_password: str = ""
    cookie_secure: bool = False
    samba_enabled: bool = True
    samba_prefix: Path = Path("/opt/samba")
    allow_ntlmv1: bool = False
    socket_path: Path = Path("/run/cammon/vfs.sock")
    frontend_dir: Path = Path("/app/frontend/dist")
    successor_quiet_seconds: int = Field(default=10, ge=0)
    idle_seal_seconds: int = Field(default=1800, ge=1)
    cache_limit_bytes: int = Field(default=10 * 1024**3, ge=0)
    cache_reserve_bytes: int = Field(default=64 * 1024**2, ge=0)
    worker_enabled: bool = True
    worker_interval_seconds: float = Field(default=5, gt=0)
    network_timeout_seconds: int = Field(default=30, ge=10)
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    ffmpeg_threads: int = Field(default=2, ge=1)
    postgres_dsn: SecretStr | None = None
    secret_key: SecretStr | None = None
    postgres_schema: str = Field(default="cammon", pattern=r"^[a-z][a-z0-9_]{0,62}$")
    metadata_sync_seconds: float = Field(default=5, gt=0)
    metadata_connect_timeout_seconds: int = Field(default=3, ge=2, le=30)
    metadata_statement_timeout_seconds: int = Field(default=5, ge=1, le=60)
    metadata_batch_size: int = Field(default=256, ge=1, le=1000)

    @property
    def cache(self) -> Path:
        return self.cache_dir or self.data_dir / "cache"

    @property
    def shares(self) -> Path:
        return self.data_dir / "shares"

    @property
    def samba_config(self) -> Path:
        return self.data_dir / "samba" / "smb.conf"
