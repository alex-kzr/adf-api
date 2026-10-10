"""Diagnostics report actual migration configuration, separately from runtime readiness."""

from fastapi.testclient import TestClient

from adf_api.main import app


def test_alive_without_migrations_is_not_ready() -> None:
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        result = client.get("/readyz")
        assert result.status_code == 503
        assert result.json()["runtime_ready"] is False
