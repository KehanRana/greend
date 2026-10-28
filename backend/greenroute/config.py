import os
from pathlib import Path
from urllib.parse import urlparse
from pydantic import BaseModel, Field, SecretStr, field_validator


class Settings(BaseModel):
    mode: str = "demo"
    gateway_key: SecretStr
    database: Path = Path("runtime/greenroute.db")
    upstream_url: str = "http://127.0.0.1:8001"
    upstream_key: SecretStr = SecretStr("demo-upstream-key")
    timeout_seconds: float = Field(default=60, gt=0, le=300)

    grid_refresh_seconds: float = Field(default=60, ge=1, le=900)
    grid_max_age_seconds: float = Field(default=300, gt=0, le=3600)
    grid_timeout_seconds: float = Field(default=5, gt=0, le=60)

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
        return cls(**{k: os.environ[f"GREENROUTE_{k.upper()}"] for k in cls.model_fields
                      if f"GREENROUTE_{k.upper()}" in os.environ})
