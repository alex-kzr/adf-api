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


def test_final_cancel_closes_nodes_only_after_stopped_proof_and_replays_sse(
    seeded: dict[str, Any],
) -> None:
    import json

    client, run_id = seeded["client"], seeded["run"]["id"]
    execution_id, lease = allocation(seeded)
    internal = "/internal/runs/" + run_id
    public = "/api/runs/" + run_id
    client.post(
        internal + "/transition", headers=seeded["orch"], json={"status": "running"}
    )
    prior = client.post(
        internal + "/nodes",
        headers=seeded["orch"],
        json={
            "node_id": "start",
            "attempt": 1,
            "status": "succeeded",
            "result": {"status": "succeeded", "evidence": "prior-success"},
        },
    ).json()
    dispatched = client.post(
        internal + "/nodes",
        headers=seeded["orch"],
        json={
            "node_id": "dev",
            "attempt": 1,
            "status": "dispatched",
            "result": {"execution_id": execution_id, "retained": "dispatch-metadata"},
        },
    ).json()
    prior_terminal = {}
    for status in ["failed", "cancelled", "skipped"]:
        node_id = "__prior_" + status
        prior_terminal[node_id] = client.post(
            internal + "/nodes",
            headers=seeded["orch"],
            json={
                "node_id": node_id,
                "attempt": 2,
                "status": status,
                "result": {"status": status, "preserved": "terminal-evidence"},
            },
        ).json()
    # A waiting node without any execution must close without invented process evidence.
    waiting = client.post(
        internal + "/nodes",
        headers=seeded["orch"],
        json={
            "node_id": "__waiting",
            "attempt": 1,
            "status": "waiting_approval",
            "result": {"approval_id": "pending-reference"},
        },
    ).json()
    worker_route = "/internal/executions/" + execution_id
    identity = {
        "pid": 123,
        "start_ticks": 900,
        "pgid": 123,
        "session_id": 123,
        "journal_id": "synthetic-supervisor",
    }
    assert (
        client.post(
            worker_route + "/heartbeat",
            headers=seeded["worker"],
            json={**lease, "process_identity": identity},
        ).status_code
        == 200
    )
    assert (
        client.post(
            public + "/cancel", headers={**seeded["admin"], "Idempotency-Key": "cancel"}
        ).status_code
        == 202
    )
    before = client.get(public, headers=seeded["admin"]).json()
    rejected = client.post(
        internal + "/transition", headers=seeded["orch"], json={"status": "cancelled"}
    )
    assert (
        rejected.status_code == 409 and rejected.json()["code"] == "EXECUTIONS_ACTIVE"
    )
    after_rejection = client.get(public, headers=seeded["admin"]).json()
    assert after_rejection["event_sequence"] == before["event_sequence"]
    assert (
        after_rejection["nodes"] == before["nodes"]
        and after_rejection["status"] == "running"
    )
    unsafe_result = client.post(
        worker_route + "/result",
        headers=seeded["worker"],
        json={**lease, "status": "cancelled", "result": {"process_stopped": False}},
    )
    assert unsafe_result.json()["code"] == "PROCESS_PROOF_REQUIRED"
    proof = {
        "process_stopped": True,
        "error_code": "CANCEL_REQUESTED",
        "commit_sha": "c" * 40,
    }
    assert (
        client.post(
            worker_route + "/result",
            headers=seeded["worker"],
            json={**lease, "status": "cancelled", "result": proof},
        ).status_code
        == 200
    )
    pre_final = client.get(public, headers=seeded["admin"]).json()
    final = client.post(
        internal + "/transition",
        headers=seeded["orch"],
        json={"status": "cancelled", "result": {"error_code": "CANCELLED"}},
    )
    assert final.status_code == 200, final.text
    snapshot = client.get(public, headers=seeded["admin"]).json()
    assert snapshot["status"] == "cancelled"
    nodes = {item["node_id"]: item for item in snapshot["nodes"]}
    assert nodes["start"] == prior
    assert all(
        nodes[node_id] == evidence for node_id, evidence in prior_terminal.items()
    )
    assert (
        nodes["dev"]["status"] == "cancelled"
        and nodes["dev"]["revision"] == dispatched["revision"] + 1
    )
    assert (
        nodes["dev"]["result"]["execution_id"] == execution_id
        and nodes["dev"]["result"]["result"] == proof
    )
    assert nodes["dev"]["result"]["retained"] == "dispatch-metadata"
    assert (
        nodes["__waiting"]["status"] == "cancelled"
        and nodes["__waiting"]["revision"] == waiting["revision"] + 1
    )
    assert (
        "process_stopped" not in nodes["__waiting"]["result"]
        and "result" not in nodes["__waiting"]["result"]
    )
    assert not any(
        item["status"] in {"dispatched", "running", "waiting_approval"}
        for item in snapshot["nodes"]
    )
    events = client.get(
        public + "/event-history",
        headers=seeded["admin"],
        params={"after": pre_final["event_sequence"]},
    ).json()["items"]
    assert [item["type"] for item in events] == [
        "node.status_changed",
        "node.status_changed",
        "run.status_changed",
    ]
    assert all(item["payload"]["status"] == "cancelled" for item in events)
    replay = client.post(
        internal + "/transition",
        headers=seeded["orch"],
        json={"status": "cancelled", "result": {"error_code": "CANCELLED"}},
    )
    assert replay.status_code == 200
    unchanged = client.get(public, headers=seeded["admin"]).json()
    assert (
        unchanged["event_sequence"] == snapshot["event_sequence"]
        and unchanged["nodes"] == snapshot["nodes"]
    )
    sse = client.get(
        public + "/events",
        headers={**seeded["admin"], "Last-Event-ID": str(pre_final["event_sequence"])},
    )
    data = [
        json.loads(line[6:])
        for line in sse.text.splitlines()
        if line.startswith("data: ")
    ]
    assert [item["event_id"] for item in data] == [item["event_id"] for item in events]
    assert (
        data[-1]["type"] == "run.status_changed"
        and data[-1]["payload"]["status"] == "cancelled"
    )
    terminal_rewrite = client.post(
        internal + "/nodes",
        headers=seeded["orch"],
        json={"node_id": "dev", "attempt": 1, "status": "succeeded", "result": {}},
    )
    assert terminal_rewrite.json()["code"] == "NODE_TERMINAL"
