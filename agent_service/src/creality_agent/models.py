from __future__ import annotations

from typing import Any

from pydantic import Field, model_validator

from .config import StrictModel


class JobRequest(StrictModel):
    request: str = Field(min_length=1, max_length=4000)
    source_url: str | None = Field(default=None, max_length=4096)
    local_path: str | None = Field(default=None, max_length=4096)
    printer_id: str | None = None
    material: str | None = None
    color: str | None = None
    settings: dict[str, Any] = Field(default_factory=dict)
    copies: int = Field(default=1, ge=1, le=100)

    @model_validator(mode="after")
    def source(self):
        if bool(self.source_url) == bool(self.local_path):
            raise ValueError("Provide exactly one model URL or local path")
        return self


class PrepareRequest(StrictModel):
    profile_id: str


class ServiceError(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


TERMINAL_STATES = {"completed", "canceled", "failed"}
ACTIVE_STATES = {"preparing", "starting", "printing", "paused", "canceling", "pausing", "resuming"}
