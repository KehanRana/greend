from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Message(StrictModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1)


class ChatRequest(StrictModel):
    model: str = Field(min_length=1)
    messages: list[Message] = Field(min_length=1, max_length=100)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=8192)
    stream: Literal[False] = False


class JobState(StrEnum):
    queued = "queued"
    waiting_for_carbon = "waiting_for_carbon"
    running = "running"
    retry_pending = "retry_pending"
    succeeded = "succeeded"
    failed = "failed"
    expired = "expired"
    cancelled = "cancelled"
    unknown = "unknown"


class JobPolicy(StrictModel):
    allowed_regions: list[str] = Field(min_length=1)
    max_carbon_gco2e_kwh: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    deadline_at: datetime
    deadline_policy: Literal["expire", "execute_best_available"]

    @field_validator("deadline_at")
    @classmethod
    def future_deadline(cls, value):
        now = datetime.now(timezone.utc)
        if value.tzinfo is None or not 0 < (value - now).total_seconds() <= 86400:
            raise ValueError("Deadline must be timezone-aware, future and within 24 hours")
        return value.astimezone(timezone.utc)


class JobSubmission(StrictModel):
    request: ChatRequest
    policy: JobPolicy


class AdapterResult(StrictModel):
    completion: dict
    upstream_request_id: str | None = None
    usage: dict | None = None


class DecisionRecord(StrictModel):
    request_id: str
    selected_endpoint: str | None
    eligible: list[str]
    excluded: dict[str, str]
    reason: str
    mode: Literal["demo"] = "demo"
    policy_version: str = "interactive-v1"
