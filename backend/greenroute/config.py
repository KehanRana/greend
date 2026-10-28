import os
from pathlib import Path
from urllib.parse import urlparse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


class Settings(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    mode: str = "demo"
    gateway_key: SecretStr
    database: Path = Path("runtime/greenroute.db")
    upstream_url: str = "http://127.0.0.1:8001"
    upstream_key: SecretStr = SecretStr("demo-upstream-key")
    timeout_seconds: float = Field(default=60, gt=0, le=300)

    grid_refresh_seconds: float = Field(default=60, ge=1, le=900)
    grid_max_age_seconds: float = Field(default=300, gt=0, le=3600)
    grid_timeout_seconds: float = Field(default=5, gt=0, le=60)

    pending_job_limit: int = Field(default=1000, ge=1, le=100000)
    worker_concurrency: int = Field(default=10, ge=1, le=100)
    interactive_concurrency: int = Field(default=10, ge=1, le=100)
    worker_poll_seconds: float = Field(default=10, ge=0.1, le=60)
    retry_base_seconds: float = Field(default=2, gt=0, le=60)
    lease_seconds: float = Field(default=120, gt=0, le=900)
    client_keys: dict[str, SecretStr] = Field(default_factory=dict)
    operator_key: SecretStr | None = None
    baseline_endpoint: str = Field(default='us-east-1',min_length=1,max_length=128)
    energy_model_version: str = Field(default='illustrative-v1',min_length=1,max_length=64)
    energy_kwh_per_1000_tokens: float = Field(default=0.001,gt=0,le=1,allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_runtime(self):
        import re
        if self.lease_seconds <= self.timeout_seconds:
            raise ValueError("Lease must exceed the total upstream timeout")
        keys = [self.gateway_key.get_secret_value()]
        for owner, key in self.client_keys.items():
            if owner == "local" or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}",owner):
                raise ValueError("Client IDs must be distinct simple names other than local")
            keys.append(key.get_secret_value())
        if self.operator_key:
            keys.append(self.operator_key.get_secret_value())
        if any(len(k) < 8 for k in keys) or len(keys) != len(set(keys)):
            raise ValueError("Client and operator keys must be distinct and at least 8 characters")
        return self

    @field_validator("mode")
    @classmethod
    def demo_only(cls, value):
        if value != "demo":
            raise ValueError("This milestone supports demo mode only")
        return value

    @field_validator("gateway_key")
    @classmethod
    def key_required(cls, value):
        if len(value.get_secret_value()) < 8:
            raise ValueError("Gateway key must contain at least 8 characters")
        return value

    @field_validator("upstream_url")
    @classmethod
    def local_mock_only(cls, value):
        parsed = urlparse(value)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {"", "/"}):
            raise ValueError("Demo upstream must be an administrator-configured local HTTP origin")
        return value.rstrip("/")

    @classmethod
    def from_env(cls):
        import json
        values = {k: os.environ[f"GREENROUTE_{k.upper()}"] for k in cls.model_fields
                  if f"GREENROUTE_{k.upper()}" in os.environ}
        if "client_keys" in values:
            values["client_keys"] = json.loads(values["client_keys"])
        return cls(**values)
