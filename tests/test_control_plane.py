"""Public CRUD, snapshot, authentication and privacy contract tests."""

from typing import Any
from uuid import uuid4

import pytest
from conftest import approve

from adf_api.models import ConfigVersion


def test_auth_scopes_and_origin(harness: dict[str, Any]) -> None:
    client = harness["client"]
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/projects").status_code == 401
    assert client.get("/api/projects", headers=harness["worker"]).status_code == 401
    assert (
        client.get("/internal/orchestrator/runs", headers=harness["admin"]).status_code
        == 401
    )
    assert (
        client.get(
            "/api/projects",
            headers={**harness["admin"], "Origin": "https://untrusted.example"},
        ).status_code
        == 403
    )
    assert client.get("/openapi.json").status_code == 401


@pytest.mark.parametrize(
    "path",
    ["/mnt/c/repo", "/home/adf/ai-factory/../escape", "/home/other/repo", "C:/repo"],
)
def test_project_rejects_non_ext4_or_unscoped_paths(
    harness: dict[str, Any], path: str
) -> None:
    response = harness["client"].post(
        "/api/projects",
        headers=harness["admin"],
        json={
            "name": "denied",
            "repo_path": path,
            "allowed_root": "/home/adf/ai-factory",
        },
    )
    assert response.status_code in {403, 422}


def test_revision_and_dependency_cycle(seeded: dict[str, Any]) -> None:
    client, admin, task = seeded["client"], seeded["admin"], seeded["task"]
    other = client.post(
        "/api/tasks",
        headers=admin,
        json={
            "project_id": task["project_id"],
            "title": "dependent",
            "dependency_ids": [task["id"]],
        },
    ).json()
    route = "/api/tasks/" + task["id"]
    assert (
        client.patch(
            route, headers=admin, json={"title": "missing revision"}
        ).status_code
        == 428
    )
    assert (
        client.patch(
            route, headers={**admin, "If-Match": "8"}, json={"title": "stale"}
        ).status_code
        == 412
    )
    assert (
        client.patch(
            route,
            headers={**admin, "If-Match": "1"},
            json={"dependency_ids": [other["id"]]},
        ).status_code
        == 422
    )
    assert (
        client.patch(
            route, headers={**admin, "If-Match": "1"}, json={"status": "done"}
        ).status_code
        == 422
    )


def test_run_idempotency_returns_same_result_and_rejects_changed_body(
    seeded: dict[str, Any],
) -> None:
    headers = {**seeded["admin"], "Idempotency-Key": "run-one"}
    duplicate = seeded["client"].post(
        "/api/runs", headers=headers, json=seeded["run_body"]
    )
    assert duplicate.status_code == 201
    assert duplicate.json()["id"] == seeded["run"]["id"]
    changed = seeded["client"].post(
        "/api/runs",
        headers=headers,
        json={**seeded["run_body"], "root_base_sha": "b" * 40},
    )
    assert changed.status_code == 409
    assert changed.json()["code"] == "IDEMPOTENCY_CONFLICT"


def test_workflow_and_agent_versions_are_immutable(seeded: dict[str, Any]) -> None:
    client, admin = seeded["client"], seeded["admin"]
    agent = seeded["agents"]["Developer"]
    updated = client.patch(
        "/api/agents/" + agent["id"],
        headers={**admin, "If-Match": "1"},
        json={"prompt": "new prompt"},
    ).json()
    assert updated["config_version_id"] != agent["config_version_id"]
    with seeded["db"].transaction() as session:
        old = session.get(ConfigVersion, agent["config_version_id"])
        assert old is not None and old.data["prompt"] == ""
    current_run = client.get("/api/runs/" + seeded["run"]["id"], headers=admin).json()
    assert current_run["agent"]["prompt"] == ""


def test_secret_manifest_and_error_input_are_not_returned(
    harness: dict[str, Any],
) -> None:
    sentinel = harness["settings"].admin_token
    response = harness["client"].post(
        "/api/mcp-servers",
        headers=harness["admin"],
        json={"name": "leaky", "manifest": {"password": sentinel}},
    )
    assert response.status_code == 422
    assert sentinel not in response.text
    response = harness["client"].post(
        "/api/projects",
        headers=harness["admin"],
        json={"name": sentinel, "invalid": sentinel},
    )
    assert response.status_code == 422 and sentinel not in response.text


def test_hash_bound_approval_and_monotonic_cancel(seeded: dict[str, Any]) -> None:
    approval = approve(seeded)
    response = seeded["client"].post(
        "/api/approvals/" + approval["id"] + "/decision",
        headers={**seeded["admin"], "Idempotency-Key": "different"},
        json={
            "decision": "reject",
            "snapshot_hash": approval["snapshot_hash"],
            "revision": 1,
        },
    )
    assert response.status_code == 409
    route = "/api/runs/" + seeded["run"]["id"]
    assert (
        seeded["client"]
        .post(
            route + "/cancel", headers={**seeded["admin"], "Idempotency-Key": "cancel"}
        )
        .status_code
        == 202
    )
    assert (
        seeded["client"]
        .post(
            route + "/resume", headers={**seeded["admin"], "Idempotency-Key": "resume"}
        )
        .json()["code"]
        == "CANCEL_MONOTONIC"
    )


def test_read_approval_cannot_authorize_writing(seeded: dict[str, Any]) -> None:
    approve(seeded, "observe")
    result = seeded["client"].post(
        "/internal/runs/" + seeded["run"]["id"] + "/executions",
        headers=seeded["orch"],
        json={
            "execution_id": str(uuid4()),
            "node_id": "dev",
            "attempt": 1,
            "operation": "agent_task",
        },
    )
    assert result.status_code == 403 and result.json()["code"] == "APPROVAL_REQUIRED"


def test_plan_edit_invalidates_old_approval(seeded: dict[str, Any]) -> None:
    approve(seeded)
    run = (
        seeded["client"]
        .get("/api/runs/" + seeded["run"]["id"], headers=seeded["admin"])
        .json()
    )
    response = seeded["client"].patch(
        "/api/runs/" + run["id"] + "/plan",
        headers={**seeded["admin"], "Idempotency-Key": "plan-edit"},
        json={
            "revision": run["revision"],
            "tasks": [
                {"id": "a", "title": "Plan A", "expected_files": ["src/a.py"]},
                {
                    "id": "b",
                    "title": "Plan B",
                    "expected_files": ["src/b.py"],
                    "dependency_ids": ["a"],
                },
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["plan_hash"] != run["plan_hash"]
    execution = seeded["client"].post(
        "/internal/runs/" + run["id"] + "/executions",
        headers=seeded["orch"],
        json={
            "execution_id": str(uuid4()),
            "node_id": "dev",
            "attempt": 1,
            "operation": "agent_task",
        },
    )
    assert execution.status_code == 403
