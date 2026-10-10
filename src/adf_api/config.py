"""Fail-closed local configuration; secrets are never returned by the API."""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.engine import make_url


def read_secret(name: str) -> str:
    path = os.getenv(name)
    return Path(path).read_text(encoding="utf-8").strip() if path else ""


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default="sqlite+pysqlite:///:memory:", repr=False)
    admin_token: str = field(default="", repr=False)
    orchestrator_token: str = field(default="", repr=False)
    worker_token: str = field(default="", repr=False)
    allowed_roots: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = (
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:8088",
        "http://localhost:8088",
    )
    artifact_root: Path = field(default_factory=lambda: Path(".artifacts"))
    worker_project_ids: tuple[str, ...] = ()
    lease_seconds: int = 45
    telemetry_enabled: bool = False
    langfuse_url: str | None = None
    langfuse_project_id: str = "adf-local"

    @classmethod
    def from_environment(cls) -> "Settings":
        url = make_url(os.getenv("DATABASE_URL", "sqlite+pysqlite:///:memory:"))
        password = read_secret("ADF_DB_PASSWORD_FILE")
        if password:
            url = url.set(password=password)
        return cls(
            database_url=url.render_as_string(hide_password=False),
            admin_token=read_secret("ADF_ADMIN_TOKEN_FILE"),
            orchestrator_token=read_secret("ADF_ORCHESTRATOR_TOKEN_FILE"),
            worker_token=read_secret("ADF_WORKER_TOKEN_FILE"),
            allowed_roots=tuple(json.loads(os.getenv("ADF_ALLOWED_ROOTS", "[]"))),
            allowed_origins=tuple(
                json.loads(
                    os.getenv("ADF_ALLOWED_ORIGINS", json.dumps(cls().allowed_origins))
                )
            ),
            artifact_root=Path(
                os.getenv("ADF_ARTIFACT_ROOT", "/var/lib/adf/artifacts")
            ),
            worker_project_ids=tuple(
                json.loads(os.getenv("ADF_WORKER_PROJECT_IDS", "[]"))
            ),
            telemetry_enabled=os.getenv("ADF_TELEMETRY_ENABLED", "false").lower()
            == "true",
            langfuse_url=os.getenv("ADF_LANGFUSE_URL"),
            langfuse_project_id=os.getenv("ADF_LANGFUSE_PROJECT_ID", "adf-local"),
        )
