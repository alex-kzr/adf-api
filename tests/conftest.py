"""Isolated HTTP/SQL fixtures: synthetic secrets and no provider/network activity."""

import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from adf_api.config import Settings
from adf_api.db import Database
from adf_api.ledger import approval_bindings
from adf_api.main import create_app
from adf_api.models import Base, Run


class TestValidator:
    def validate(self, graph: dict[str, Any]) -> dict[str, Any]:
        # Control-plane tests inject the protocol; graph semantic tests belong to orchestrator.
        valid = isinstance(graph.get("nodes"), list) and any(
            node.get("type") == "start" for node in graph["nodes"]
        )
        return {"valid": valid, "errors": [] if valid else ["missing start"]}


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[dict[str, Any]]:
    settings = Settings(
        admin_token=secrets.token_urlsafe(32),
        orchestrator_token=secrets.token_urlsafe(32),
        worker_token=secrets.token_urlsafe(32),
        allowed_roots=("/home/adf/ai-factory",),
        artifact_root=tmp_path / "artifacts",
    )
    database = Database(settings.database_url)
    Base.metadata.create_all(
        database.engine
    )  # Explicitly test-only; deployment runs Alembic.
    with TestClient(create_app(settings, database, TestValidator())) as client:
        yield {
            "client": client,
            "db": database,
            "settings": settings,
            "admin": {"Authorization": "Bearer " + settings.admin_token},
            "orch": {"Authorization": "Bearer " + settings.orchestrator_token},
            "worker": {"Authorization": "Bearer " + settings.worker_token},
        }
    database.engine.dispose()


@pytest.fixture
def seeded(harness: dict[str, Any]) -> dict[str, Any]:
    client, admin = harness["client"], harness["admin"]
    project = client.post(
        "/api/projects",
        headers=admin,
        json={
            "name": "test",
            "repo_path": "/home/adf/ai-factory/projects/test/repo",
            "allowed_root": "/home/adf/ai-factory",
            "trusted": True,
            "checks": [{"id": "unit", "argv": ["python", "-m", "unittest"]}],
        },
    ).json()
    task = client.post(
        "/api/tasks",
        headers=admin,
        json={
            "project_id": project["id"],
            "title": "Implement feature",
            "description": "A trusted test task",
            "expected_files": ["src/a.py"],
            "status": "ready",
        },
    ).json()
    agents = {}
    for role in ["Planner", "Developer", "Reviewer", "Tester", "Integrator"]:
        agents[role] = client.post(
            "/api/agents",
            headers=admin,
            json={
                "name": role,
                "role": role,
                "provider": "mock",
                "permission_profile": "observe"
                if role in {"Planner", "Reviewer"}
                else "trusted-development",
            },
        ).json()
    policy = client.post(
        "/api/policies",
        headers=admin,
        json={
            "name": "Trusted tests",
            "manifest": {
                "allow_commit": True,
                "trusted_repository": True,
                "allowed_write_paths": ["src/**"],
            },
        },
    ).json()
    graph = {
        "schema_version": "1",
        "nodes": [
            {"id": "start", "type": "start", "config": {}},
            {"id": "dev", "type": "agent_task", "config": {}},
            {"id": "end", "type": "end", "config": {}},
        ],
        "edges": [
            {"source": "start", "target": "dev"},
            {"source": "dev", "target": "end"},
        ],
    }
    workflow = client.post(
        "/api/workflows",
        headers=admin,
        json={"name": "Test workflow", "graph_json": graph},
    ).json()
    version = client.post(
        "/api/workflows/" + workflow["id"] + "/versions", headers=admin
    ).json()
    body = {
        "task_id": task["id"],
        "workflow_version_id": version["id"],
        "agent_config_version_id": agents["Developer"]["config_version_id"],
        "policy_id": policy["id"],
        "root_base_sha": "a" * 40,
    }
    run_response = client.post(
        "/api/runs", headers={**admin, "Idempotency-Key": "run-one"}, json=body
    )
    assert run_response.status_code == 201, run_response.text
    run = run_response.json()
    harness.update(
        project=project,
        task=task,
        agents=agents,
        workflow=workflow,
        version=version,
        policy=policy,
        run=run,
        run_body=body,
    )
    return harness


def approve(harness: dict[str, Any], action: str = "execute_plan") -> dict[str, Any]:
    run = harness["run"]
    with harness["db"].transaction() as session:
        snapshot = {**approval_bindings(session.get(Run, run["id"])), "action": action}
    response = harness["client"].post(
        "/internal/runs/" + run["id"] + "/approvals",
        headers=harness["orch"],
        json={"action": action, "snapshot": snapshot},
    )
    assert response.status_code == 201, response.text
    approval = response.json()
    response = harness["client"].post(
        "/api/approvals/" + approval["id"] + "/decision",
        headers={**harness["admin"], "Idempotency-Key": "approval-" + approval["id"]},
        json={
            "decision": "approve",
            "snapshot_hash": approval["snapshot_hash"],
            "revision": approval["revision"],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()
