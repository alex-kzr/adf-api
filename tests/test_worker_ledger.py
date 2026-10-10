"""Fencing, redaction, replay, artifacts, reservations and stopped-process evidence."""

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from conftest import approve

from adf_api.models import Execution


def allocation(
    seeded: dict[str, Any], operation: str = "agent_task"
) -> tuple[str, dict[str, Any]]:
    approve(seeded)
    client = seeded["client"]
    registration = {
        "worker_id": "test-worker",
        "capabilities": {"mock": {"ready": True}},
        "allowed_roots": ["/home/adf/ai-factory"],
    }
    assert (
        client.post(
            "/internal/workers/register", headers=seeded["worker"], json=registration
        ).status_code
        == 200
    )
    execution_id = str(uuid4())
    response = client.post(
        "/internal/runs/" + seeded["run"]["id"] + "/executions",
        headers=seeded["orch"],
        json={
            "execution_id": execution_id,
            "node_id": "dev",
            "attempt": 1,
            "operation": operation,
        },
    )
    assert response.status_code == 201, response.text
    claim = client.post(
        "/internal/executions/" + execution_id + "/claim",
        headers=seeded["worker"],
        json={"worker_id": "test-worker"},
    )
    assert claim.status_code == 200, claim.text
    return execution_id, {"worker_id": "test-worker", "epoch": claim.json()["epoch"]}


def test_duplicate_claim_and_events_are_exactly_once(seeded: dict[str, Any]) -> None:
    execution_id, lease = allocation(seeded)
    route = "/internal/executions/" + execution_id
    again = (
        seeded["client"]
        .post(
            route + "/claim",
            headers=seeded["worker"],
            json={"worker_id": "test-worker"},
        )
        .json()
    )
    assert again["epoch"] == lease["epoch"]
    event = {
        "event_id": str(uuid4()),
        "sequence": 1,
        "type": "output",
        "payload": {"text": "safe"},
    }
    body = {**lease, "events": [event]}
    first = seeded["client"].post(
        route + "/events", headers=seeded["worker"], json=body
    )
    second = seeded["client"].post(
        route + "/events", headers=seeded["worker"], json=body
    )
    assert first.status_code == second.status_code == 200
    assert (
        first.json()["items"][0]["run_sequence"]
        == second.json()["items"][0]["run_sequence"]
    )
    conflict = seeded["client"].post(
        route + "/events",
        headers=seeded["worker"],
        json={**lease, "events": [{**event, "payload": {"text": "changed"}}]},
    )
    assert conflict.status_code == 409


def test_expired_lease_is_uncertain_and_cannot_reassign(seeded: dict[str, Any]) -> None:
    execution_id, lease = allocation(seeded)
    with seeded["db"].transaction() as session:
        row = session.get(Execution, execution_id)
        assert row is not None
        row.lease_expires_at = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    route = "/internal/executions/" + execution_id
    assert (
        seeded["client"]
        .post(route + "/heartbeat", headers=seeded["worker"], json=lease)
        .json()["code"]
        == "EXECUTION_UNCERTAIN"
    )
    with seeded["db"].transaction() as session:
        row = session.get(Execution, execution_id)
        assert row is not None and row.status == "uncertain"
    assert (
        seeded["client"]
        .post(
            route + "/claim",
            headers=seeded["worker"],
            json={"worker_id": "test-worker"},
        )
        .status_code
        == 409
    )
    assert (
        seeded["client"]
        .post(
            route + "/result",
            headers=seeded["worker"],
            json={**lease, "status": "succeeded", "result": {}},
        )
        .status_code
        == 409
    )
    assert (
        seeded["client"]
        .post(
            route + "/reconcile",
            headers=seeded["worker"],
            json={
                **lease,
                "process_stopped": True,
                "evidence": {
                    "journal_id": "execution-journal",
                    "worktree_lock_reacquired": True,
                },
            },
        )
        .status_code
        == 200
    )


def test_stale_epoch_and_process_identity_denied(seeded: dict[str, Any]) -> None:
    execution_id, lease = allocation(seeded)
    route = "/internal/executions/" + execution_id
    assert (
        seeded["client"]
        .post(
            route + "/heartbeat", headers=seeded["worker"], json={**lease, "epoch": 2}
        )
        .json()["code"]
        == "LEASE_STALE"
    )
    identity = {"pid": 123, "start_ticks": 10000, "pgid": 123}
    assert (
        seeded["client"]
        .post(
            route + "/heartbeat",
            headers=seeded["worker"],
            json={**lease, "process_identity": identity},
        )
        .status_code
        == 200
    )
    assert (
        seeded["client"]
        .post(
            route + "/result",
            headers=seeded["worker"],
            json={**lease, "status": "failed", "result": {"process_stopped": False}},
        )
        .status_code
        == 409
    )
    assert (
        seeded["client"]
        .post(
            route + "/reconcile",
            headers=seeded["worker"],
            json={
                **lease,
                "process_stopped": True,
                "evidence": {
                    "journal_id": "journal",
                    "worktree_lock_reacquired": True,
                    "pid": 124,
                    "start_ticks": 10000,
                    "pgid": 124,
                },
            },
        )
        .status_code
        == 409
    )


def test_result_before_ack_is_idempotent_and_terminal_immutable(
    seeded: dict[str, Any],
) -> None:
    execution_id, lease = allocation(seeded)
    route = "/internal/executions/" + execution_id + "/result"
    body = {
        **lease,
        "status": "succeeded",
        "result": {"tree_sha": "b" * 40, "usage": {"input_tokens": None, "cost": None}},
    }
    assert (
        seeded["client"]
        .post(route, headers=seeded["worker"], json=body)
        .json()["persisted"]
        is True
    )
    assert (
        seeded["client"].post(route, headers=seeded["worker"], json=body).status_code
        == 200
    )
    assert (
        seeded["client"]
        .post(route, headers=seeded["worker"], json={**body, "status": "failed"})
        .status_code
        == 409
    )


def test_cancel_during_result_preserves_commit_but_cancels(
    seeded: dict[str, Any],
) -> None:
    execution_id, lease = allocation(seeded)
    seeded["client"].post(
        "/api/runs/" + seeded["run"]["id"] + "/cancel",
        headers={**seeded["admin"], "Idempotency-Key": "cancel"},
    )
    response = seeded["client"].post(
        "/internal/executions/" + execution_id + "/result",
        headers=seeded["worker"],
        json={
            **lease,
            "status": "succeeded",
            "result": {"commit_sha": "c" * 40, "process_stopped": True},
        },
    )
    assert response.json()["status"] == "cancelled"
    with seeded["db"].transaction() as session:
        row = session.get(Execution, execution_id)
        assert row is not None and row.result["commit_sha"] == "c" * 40


def test_event_and_artifact_secret_redaction_and_safe_download(
    seeded: dict[str, Any],
) -> None:
    execution_id, lease = allocation(seeded)
    route = "/internal/executions/" + execution_id
    sentinel = seeded["settings"].admin_token
    event = {
        "event_id": str(uuid4()),
        "sequence": 1,
        "type": "output",
        "payload": {"text": "sentinel " + sentinel, "authorization": sentinel},
    }
    response = seeded["client"].post(
        route + "/events", headers=seeded["worker"], json={**lease, "events": [event]}
    )
    assert response.status_code == 200 and sentinel not in response.text
    content = ("sentinel " + sentinel).encode()
    artifact = seeded["client"].post(
        route + "/artifacts",
        headers=seeded["worker"],
        json={
            **lease,
            "artifact_id": str(uuid4()),
            "kind": "log",
            "media_type": "text/plain",
            "content_base64": base64.b64encode(content).decode(),
            "checksum": hashlib.sha256(content).hexdigest(),
        },
    )
    assert artifact.status_code == 201, artifact.text
    route = "/api/artifacts/" + artifact.json()["id"] + "/content"
    assert seeded["client"].get(route).status_code == 401
    download = seeded["client"].get(route, headers=seeded["admin"])
    assert sentinel not in download.text
    assert "storage_key" not in artifact.json()
    assert download.headers["content-disposition"].startswith("attachment")


def test_pause_stops_dispatch_and_completion_requires_integration(
    seeded: dict[str, Any],
) -> None:
    route = "/api/runs/" + seeded["run"]["id"]
    seeded["client"].post(
        route + "/pause", headers={**seeded["admin"], "Idempotency-Key": "pause"}
    )
    execution = seeded["client"].post(
        "/internal/runs/" + seeded["run"]["id"] + "/executions",
        headers=seeded["orch"],
        json={
            "execution_id": str(uuid4()),
            "node_id": "dev",
            "attempt": 1,
            "operation": "repo_inspect",
        },
    )
    assert (
        execution.status_code == 409 and execution.json()["code"] == "CONTROL_PENDING"
    )
    seeded["client"].post(
        route + "/resume", headers={**seeded["admin"], "Idempotency-Key": "resume"}
    )
    seeded["client"].post(
        "/internal/runs/" + seeded["run"]["id"] + "/transition",
        headers=seeded["orch"],
        json={"status": "running"},
    )
    response = seeded["client"].post(
        "/internal/runs/" + seeded["run"]["id"] + "/transition",
        headers=seeded["orch"],
        json={"status": "completed"},
    )
    assert (
        response.status_code == 409 and response.json()["code"] == "EVIDENCE_REQUIRED"
    )
