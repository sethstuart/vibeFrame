from __future__ import annotations

from datetime import time
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Orientation = Literal[0, 90, 180, 270]
DriverName = Literal["auto", "mock", "inky"]
SelectionMode = Literal["shuffle", "sequential", "favorites", "recent"]
DitherName = Literal["floyd-steinberg", "atkinson", "bayer", "none"]
CropMode = Literal["smart", "center", "fit"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VIBEFRAME_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    photos_dir: Path = Path("/photos")
    upload_subdir: str = "_uploads"
    cache_dir: Path = Path("/var/cache/vibeframe")
    state_dir: Path = Path("/var/lib/vibeframe")
    recursive: bool = True

    driver: DriverName = "auto"
    orientation: Orientation = 270
    refresh_seconds: int = Field(default=1800, ge=10)
    selection_mode: SelectionMode = "shuffle"

    quiet_start: time = time(22, 0)
    quiet_end: time = time(7, 0)
    tz: str = "UTC"

    dither: DitherName = "floyd-steinberg"
    crop_mode: CropMode = "smart"
    saturation: float = Field(default=1.15, ge=0.0, le=3.0)
    contrast: float = Field(default=1.05, ge=0.0, le=3.0)

    web_host: str = "0.0.0.0"
    web_port: int = Field(default=8080, ge=1, le=65535)
    web_token: str | None = None

    log_level: str = "INFO"
    cache_max_bytes: int = Field(default=500 * 1024 * 1024, ge=1024 * 1024)

    @field_validator("tz")
    @classmethod
    def _validate_tz(cls, v: str) -> str:
        ZoneInfo(v)
        return v

    @field_validator("orientation", mode="before")
    @classmethod
    def _coerce_orientation(cls, v):
        if isinstance(v, str) and v.strip().lstrip("-").isdigit():
            return int(v)
        return v

    @property
    def upload_dir(self) -> Path:
        return self.photos_dir / self.upload_subdir

    @property
    def mock_dir(self) -> Path:
        return self.state_dir / "mock"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "vibeframe.db"

    @property
    def zoneinfo(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    def ensure_dirs(self) -> None:
        for p in (self.cache_dir, self.state_dir, self.mock_dir):
            p.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_for_tests() -> None:
    global _settings
    _settings = None
