"""Only selected, lease-bound worker metadata may become reviewer evidence."""

from typing import Any
from uuid import uuid4

from test_worker_ledger import allocation

from adf_api.models import Event, Execution


def test_internal_execution_has_bounded_selected_capability_evidence(
    seeded: dict[str, Any],
) -> None:
    execution_id, lease = allocation(seeded)
    with seeded["db"].transaction() as session:
        row = session.get(Execution, execution_id)
        assert row is not None
        agent = {
            **row.request["agent"],
            "skills": [{"manifest": {"hash": "a" * 64}}],
            "mcp_servers": [
                {"name": "markers", "manifest": {"allowed_tools": ["read_marker"]}}
            ],
        }
        row.request = {**row.request, "agent": agent}
    payloads = [
        (
            "skill_loaded",
            {
                "name": "selected",
                "sha256": "a" * 64,
                "native": True,
                "content": "DO_NOT_FORWARD",
            },
        ),
        (
            "tool_finished",
            {
                "tool_id": "mcp_tool_call",
                "server_id": "markers",
                "tool_name": "read_marker",
                "status": "completed",
                "raw": "DO_NOT_FORWARD",
            },
        ),
        (
            "tool_finished",
            {
                "tool_id": "mcp_tool_call",
                "server_id": "unassigned",
                "tool_name": "read_marker",
                "status": "completed",
            },
        ),
        ("output", {"text": "DO_NOT_FORWARD"}),
    ]
    events = [
        {"event_id": str(uuid4()), "sequence": i, "type": kind, "payload": payload}
        for i, (kind, payload) in enumerate(payloads, 1)
    ]
    response = seeded["client"].post(
        "/internal/executions/" + execution_id + "/events",
        headers=seeded["worker"],
        json={**lease, "events": events},
    )
    assert response.status_code == 200
    with seeded["db"].transaction() as session:
        session.add(
            Event(
                run_id=seeded["run"]["id"],
                execution_id=execution_id,
                epoch=lease["epoch"] - 1,
                sequence=1,
                run_sequence=9999,
                type="skill_loaded",
                payload={"name": "stale", "sha256": "a" * 64, "native": True},
            )
        )
    result = (
        seeded["client"]
        .get("/internal/executions/" + execution_id, headers=seeded["orch"])
        .json()
    )
    evidence = result["capability_audit"]
    assert (
        evidence["execution_id"] == execution_id and evidence["epoch"] == lease["epoch"]
    )
    assert evidence["run_id"] == seeded["run"]["id"]
    assert not evidence["truncated"] and len(evidence["events"]) == 2
    assert {e["kind"] for e in evidence["events"]} == {"skill_loaded", "mcp_tool_call"}
    assert "DO_NOT_FORWARD" not in str(evidence) and "stale" not in str(evidence)
    assert "unassigned" not in str(evidence)
