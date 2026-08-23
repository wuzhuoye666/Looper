from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="LOOPER_",
        extra="ignore",
        case_sensitive=False,
    )

    host: str = "127.0.0.1"
    port: int = 8000
    remote_worker_api_url: AnyHttpUrl | None = None
    remember_ssh_credentials: bool = True
    default_ssh_username: str = "root"
    default_ssh_port: int = Field(default=22, ge=1, le=65535)
    default_ssh_auth_method: Literal["password", "private-key"] = "private-key"
    default_ssh_private_key_path: str = ""
    default_ssh_password: SecretStr | None = Field(default=None, repr=False)
    data_dir: Path = Path(".looper")
    database_url: str | None = None
    allowed_origins: str = "http://127.0.0.1:5173,http://localhost:5173"
    trusted_hosts: str = "127.0.0.1,localhost,testserver"
    local_worker_token: str = "looper-local-development"
    max_artifact_bytes: int = Field(default=256 * 1024 * 1024, ge=1024)
    max_output_bytes: int = Field(default=16 * 1024 * 1024, ge=1024)
    lease_seconds: int = Field(default=30, ge=5, le=3600)
    worker_stale_seconds: int = Field(default=90, ge=10, le=86400)
    max_local_workers: int = Field(default=4, ge=1, le=64)
    cloud_catalog_ttl_seconds: int = Field(default=300, ge=30, le=86400)
    cloud_stale_cache_seconds: int = Field(default=86400, ge=300, le=604800)
    cloud_quote_ttl_seconds: int = Field(default=300, ge=60, le=3600)
    purchase_confirmation_seconds: int = Field(default=1800, ge=60, le=3600)
    live_purchase_enabled: bool = False
    live_purchase_providers: str = ""
    purchase_confirmation_secret: str = Field(
        default="change-me-before-enabling-live-purchase", repr=False
    )
    operator_token: str = Field(default="", repr=False)
    max_live_hourly_amount: Decimal = Field(default=Decimal("10"), gt=0)
    deepseek_base_url: AnyHttpUrl = "https://api.deepseek.com"
    deepseek_api_key: str = Field(default="", repr=False)
    deepseek_model: str = "deepseek-v4-flash"
    source_discovery_max_archive_bytes: int = Field(default=20 * 1024 * 1024, ge=1024)
    source_discovery_max_expanded_bytes: int = Field(default=100 * 1024 * 1024, ge=1024)
    source_discovery_max_files: int = Field(default=10_000, ge=1, le=100_000)
    source_discovery_max_tool_rounds: int = Field(default=32, ge=1, le=128)
    source_discovery_max_output_tokens: int = Field(default=16384, ge=1024, le=32768)
    alibaba_default_region: str = "cn-hangzhou"

    @field_validator("remote_worker_api_url", mode="before")
    @classmethod
    def empty_remote_worker_api_url_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def database_uri(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'looper.db').as_posix()}"

    @property
    def origin_list(self) -> list[str]:
        return [value.strip() for value in self.allowed_origins.split(",") if value.strip()]

    @property
    def trusted_host_list(self) -> list[str]:
        hosts = {value.strip() for value in self.trusted_hosts.split(",") if value.strip()}
        if self.remote_worker_api_url and self.remote_worker_api_url.host:
            hosts.add(self.remote_worker_api_url.host)
        return sorted(hosts)

    @property
    def enabled_purchase_providers(self) -> set[str]:
        return {
            value.strip().lower()
            for value in self.live_purchase_providers.split(",")
            if value.strip()
        }

    @property
    def artifact_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def remote_credential_key_path(self) -> Path:
        return self.data_dir / "remote-worker-credentials.key"

    @property
    def remote_credential_store_path(self) -> Path:
        return self.data_dir / "remote-worker-credentials.json"

    @property
    def deepseek_credential_key_path(self) -> Path:
        return self.data_dir / "deepseek-credential.key"

    @property
    def deepseek_credential_store_path(self) -> Path:
        return self.data_dir / "deepseek-credential.enc"

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
