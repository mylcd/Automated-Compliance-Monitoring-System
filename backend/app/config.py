from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file="../.env", extra="ignore", hide_input_in_errors=True)

    database_url: str = "sqlite:///../data/myl.db"
    evidence_dir: Path = Path("../evidence/runtime")
    admin_username: str = Field(default="admin", min_length=1, max_length=80)
    admin_password: SecretStr
    app_origin: str = "http://localhost:8080"
    session_hours: int = Field(default=8, ge=1, le=168)
    cookie_secure: bool = False

    @field_validator("admin_password")
    @classmethod
    def validate_password(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip() or value.get_secret_value().startswith("replace-"):
            raise ValueError("Configure a nonempty administrator password")
        if len(value.get_secret_value()) > 1024:
            raise ValueError("Administrator password must be at most 1024 characters")
        return value

    @field_validator("database_url")
    @classmethod
    def sqlite_only(cls, value: str) -> str:
        if not value.startswith("sqlite:///"):
            raise ValueError("This local demo requires a SQLite database URL")
        return value
