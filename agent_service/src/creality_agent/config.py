from __future__ import annotations

import json
import os
import secrets
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Policy(StrictModel):
    max_hours: float = Field(default=8, gt=0, le=168)
    max_grams: float = Field(default=250, gt=0, le=10000)
    limits_confirmed: bool = True
    monitoring_loss_seconds: float = Field(default=60, gt=0, le=3600)
    pause_on_monitoring_loss: bool = False
    monitoring_policy_confirmed: bool = True
    max_download_bytes: int = 256 * 1024 * 1024
    max_expanded_bytes: int = 1024 * 1024 * 1024
    max_files: int = 100
    frame_max_age_seconds: float = 10


class CFSSlot(StrictModel):
    slot_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    material: str | None = None
    color: str | None = None
    remaining_grams: float | None = Field(default=None, ge=0)
    verified: bool = False


class LocalModel(StrictModel):
    base_url: str = "http://127.0.0.1:8000/v1"
    model: str = ""
    api_key: str | None = None
    max_output_tokens: int = Field(default=12000, ge=256, le=128000)


class Printer(StrictModel):
    id: str
    name: str
    model: str
    endpoint: str | None = None
    api_key_env: str | None = None
    nozzle_mm: float | None = None
    cfs: bool | None = None
    identity_confirmed: bool = False
    protocol_qualified: bool = False
    control_qualified: bool = False
    camera_url: str | None = None
    camera_association_confirmed: bool = False
    vision_qualified: bool = False
    bed_detector_url: str | None = None
    baseline_file: str | None = None
    roi: tuple[int, int, int, int] | None = None
    frame_sequence_header: str | None = None
    frame_time_header: str | None = None
    material: str | None = None
    color: str | None = None
    filament_verified: bool = False
    remaining_grams: float | None = None
    failure_detector_url: str | None = None
    failure_detector_qualified: bool = False
    auto_start: bool = False
    cfs_slots: list[CFSSlot] = Field(default_factory=list)


class Profile(StrictModel):
    id: str
    printer_id: str
    settings: list[str]
    filaments: list[str]
    material: str
    color: str | None = None
    verified: bool = False
    nozzle_mm: float


class Settings(StrictModel):
    bind: str = "127.0.0.1"
    port: int = Field(default=18088, ge=1024, le=65535)
    allowed_origins: list[str] = Field(default_factory=list)
    public_base_url: str = "http://127.0.0.1:18088"
    slicer_binary: str = "/Applications/Creality Print.app/Contents/MacOS/CrealityPrint"
    gui_helper_binary: str | None = None
    notifications_enabled: bool = True
    local_model: LocalModel = Field(default_factory=LocalModel)
    import_roots: list[str] = Field(default_factory=list)
    policy: Policy = Field(default_factory=Policy)
    printers: list[Printer] = Field(default_factory=list)
    profiles: list[Profile] = Field(default_factory=list)


def load_settings(home: Path) -> Settings:
    return Settings.model_validate_json((home / "config.json").read_text())


def initialize(home: Path) -> Settings:
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(home, 0o700)
    config = home / "config.json"
    if not config.exists():
        fleet = [("k1-max-1", "K1 Max"), *[(f"k1-se-{i}", "K1 SE") for i in range(1, 4)],
                 ("hi-1", "Creality Hi"), ("hi-2", "Creality Hi")]
        settings = Settings(printers=[Printer(id=id, name=id, model=model) for id, model in fleet])
        config.write_text(json.dumps(settings.model_dump(), indent=2) + "\n")
        os.chmod(config, 0o600)
    for name in ("agent.token", "owner.token"):
        path = home / name
        if not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_urlsafe(36) + "\n")
    return load_settings(home)


def save_settings(home: Path, settings: Settings) -> None:
    """Replace private configuration atomically; never publish credentials to the UI."""
    fd, name = tempfile.mkstemp(prefix=".config-", dir=home)
    try:
        with os.fdopen(fd, "w") as file:
            os.fchmod(file.fileno(), 0o600)
            file.write(settings.model_dump_json(indent=2) + "\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, home / "config.json")
    finally:
        Path(name).unlink(missing_ok=True)
