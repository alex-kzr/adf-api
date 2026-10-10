# Implementation wire contract v1

All JSON uses snake_case, UUID strings and UTC timestamps. Admin routes use the admin bearer token; internal orchestrator routes use a separate orchestrator token; worker routes use a worker token plus a registered worker identity. Configuration: DATABASE_URL, ADF_ADMIN_TOKEN_FILE, ADF_ORCHESTRATOR_TOKEN_FILE, ADF_WORKER_TOKEN_FILE, ADF_ALLOWED_ROOTS (JSON array), ADF_ARTIFACT_ROOT. No login endpoint: UI holds a manually entered bearer token in memory.

## Definitions

GET lists return `{items,next_cursor}` with optional project_id/run_id/status filters and limit (max 200). GET detail returns the record. PATCH requires `If-Match: <revision>`.

- projects: `{name,repo_path,default_branch:"main",allowed_root,checks:[{id,argv,timeout_sec}],trusted:true}`. Paths must be Linux absolute beneath an allowed root; worker proves canonical path/Git validity before execution.
- tasks: `{project_id,title,description,acceptance_criteria:[string],priority:0,status:"draft"|"ready",dependency_ids:[],expected_files:[]}`. Dependencies are same-project and acyclic. PATCH cannot manufacture running/done state.
- agents: `{name,role,provider:"codex"|"claude"|"mock",model:null,prompt,skill_ids:[],tool_ids:[],mcp_server_ids:[],permission_profile:"observe"|"trusted-development",limits:{...}}`. Every edit creates an immutable config_version_id/config_hash.
- workflows: `{name,graph_json:{schema_version:"1",nodes:[{id,type,config}],edges:[{id,source,target,source_port?,target_port?}]}}`; POST validate returns `{valid,errors}`; POST versions publishes validated immutable `{id,workflow_id,version,graph_json,graph_hash}`.
- skills/tools/mcp-servers/policies: typed `{name,manifest}` definitions, immutable version/hash. No secret values or arbitrary shell strings. Skill manifest includes trusted path/version/hash; tools include input_schema/permission/provider_mappings. Claude read mapping is {"claude":["Read","Glob","Grep"],"mock":[]}. Codex read mappings name exact assigned MCP tools, for example {"codex":["mcp__factoryfiles__read_marker"],"mock":[]}. Codex builtin/shell mappings are rejected. MCP includes transport:"stdio", argv, trusted:true, scope and allowed_tools.

## Public runtime

POST /api/runs `{task_id,workflow_version_id,agent_config_version_id?,policy_id?,root_base_sha?,plan?}` with Idempotency-Key. Returns queued run snapshot. `/api/tasks/{id}/decompose` accepts `{agent_config_version_id,workflow_version_id?,policy_id?}` and creates a queued planning run/command, never approval. POST `/api/runs/{id}/pause|resume|cancel` with Idempotency-Key returns 202 `{command_id,run_id,requested}`. GET detail includes pinned project/task/workflow/agent/policy, nodes/executions/approvals, controls and results. GET nodes/artifacts/git-changes/approvals and GET fetch SSE events are available. SSE ids are monotonic run_sequence; Last-Event-ID replays and terminal EOF follows final event. `/api/runs/{id}/event-history` exposes the same envelope in a paginated JSON list.

POST `/api/approvals/{id}/decision` `{decision:"approve"|"reject",snapshot_hash,revision}` plus Idempotency-Key, response immutable approval. Approval exact snapshot requires action, graph_hash, plan_hash, root_base_sha, policy_hash, dependency_hash, agent_config_hash, agent_versions_hash, allowed_write_paths and tool_ids. Missing or changed fields cannot authorize writes.

## Orchestrator ledger facade

GET `/internal/orchestrator/runs?status=active` returns `{items,next_cursor}` with full run snapshots. GET `/internal/orchestrator/commands` lists pending commands; POST `/internal/commands/{id}/applied` stores result. GET `/internal/orchestrator/outbox` lists pending durable dispatches; POST `/internal/outbox/{id}/delivered` marks transport publication, replay remains safe.

POST `/internal/runs/{id}/transition` `{status,revision?,integration_status?,result?}` guarded by controls and evidence. POST `/nodes` `{node_id,attempt:1,status,result?}` creates/updates a node ledger. POST `/executions` `{execution_id,node_id,attempt,operation,task_id?,agent_config_version_id?,dependency_execution_ids:[],request:{...}}` idempotently creates execution + outbox. Request includes immutable execution base, root base, prompt, limits, check IDs, task branch/worktree key; API overlays trusted project/agent/policy snapshots. Active reservations enforce max_parallel=3 and expected file overlap; internal writing dependencies require succeeded accepted git_commit records, not mere successful agent/test/review. External manual dependencies require completed integration before run creation. POST `/approvals` `{action,snapshot,expires_at?}` returns hash-bound pending approval; GET `/internal/approvals/{id}`. POST `/events` `{event_id,type,payload}`. POST `/plan` `{tasks:[{id?,title,description,acceptance_criteria,expected_files,dependency_ids}],source_plan_hash?}` records validated decomposition/tasks/plan snapshot. Exact replay preserves mapped UUIDs. GET `/internal/executions/{id}` returns execution+request/result for orchestration.

## Worker

Redis stream `adf:executions`, consumer group `adf-workers`; fields `{execution_id,dispatch_id}` only. API never trusts transport-provided paths/commands.

POST `/internal/workers/register` `{worker_id,capabilities,allowed_roots,project_ids:[]}`. POST `/internal/workers/{id}/heartbeat` `{running_executions:[]}`.

POST `/internal/executions/{id}/claim` `{worker_id}` returns `{execution_id,epoch,lease_expires_at,request,status,terminal}`. Request overlays `{project:{repo_path,allowed_root,default_branch,checks/check_registry,check_timeouts},agent:{id,agent_id,config_version_id,provider,model,prompt,permission_profile,allowed_tools,skills,mcp_servers},policy:{allowed_write_paths,allow_commit,approved,...}}`. Terminal duplicate returns terminal=true; expired lease becomes uncertain and is never assigned again without stopped proof.

POST heartbeat `{worker_id,epoch,process_identity?}` extends lease 45 seconds; GET control `?worker_id=&epoch=` returns `{status,pause_requested,cancel_requested,lease_expires_at}`. POST events `{worker_id,epoch,events:[{event_id,sequence,type,time?,payload}]}` allocates run sequence; exact duplicates no-op. POST result `{worker_id,epoch,status:"succeeded"|"failed"|"cancelled"|"uncertain",result:{commit_sha,base_sha,tree_sha,branch,worktree_key,test_results,review,usage,artifacts,...}}`. Exact terminal duplicate is ACK-safe; stale owner rejects.

POST reconcile `{worker_id,epoch,process_stopped:true,evidence:{journal_id,pid,start_ticks,pgid,session_id,child_group_stopped:true,worktree_lock_reacquired:true}}` only same owner, after supervisor proves death; uncertain becomes failed/cancelled and is never silently dispatched again. POST artifacts `{worker_id,epoch,kind,media_type,content_base64,sha256}` stores bounded redacted bytes under a generated opaque key. Worker artifact upload capped 20MiB; no host path accepted. GET /api/artifacts/{id}/content is authenticated safe attachment.

Errors `{code,message,details,request_id}`. Lease/auth/config/privacy checks fail closed. All IDs and snapshots can be read by orchestrator; public responses exclude task prompts and raw internal request. Whole MVP readiness remains umbrella acceptance, separate from API DB readiness.

## Durable metadata and human revisions

POST `/internal/runs/{id}/base` pins a full root Git commit SHA exactly once. Read-only `repo_inspect` can precede human approval; later writes bind that base. POST `/internal/runs/{id}/control` records orchestrator cancellation on the same durable command ledger.

POST `/internal/runs/{id}/trace` `{trace_id,traceparent}` performs immutable compare-and-set and returns the stored winner. POST `/internal/runs/{id}/graph-proposal` `{plan_hash,graph_json}` semantically validates and stores `run.result.graph_proposal` plus `graph_proposal_plan_hash`, separately from the plan digest.

PATCH `/api/runs/{id}/plan` `{revision,tasks:[...]}` with Idempotency-Key persists a bounded acyclic plan, invalidates old approval hashes, clears stale graph proposals and enqueues `refresh_proposal`. Completed planning runs can produce revised proposals; writing evidence makes the plan immutable. Publish the proposal as a new workflow version and start a separate execution run with the exact plan/base/policy.

POST `/api/runs/{id}/resolution` `{execution_id,tree_sha}` with Idempotency-Key creates a separate integration-resolution approval for a paused conflict. `run.resolution` stores `{execution_id,tree_sha,approval_id}`. Decide that approval, then explicitly resume. Ordinary resume cannot authorize a changed tree.

POST `/internal/workers/{id}/dead-letter` `{message_id,stream:"adf:executions",reason,field_names}` persists an idempotent sanitized audit marker before poison-message acknowledgment; raw field values and exception text are discarded.

Terminal test/review/commit evidence must match the requested target tree, including stable distinct developer/reviewer identities. Integration accepts the complete selected accepted commit set; all registered checks must pass on its final tree. Completion derives accepted child task states only from commits included in that integration.

Event payloads are limited to 60,000 UTF-8 bytes so the complete SSE envelope/framing stays below 64 KiB. Ordinary/chunked requests are limited to 1 MiB; artifact base64 envelopes to 30 MiB, decoded content to 20 MiB. Artifact bytes are redacted before checksummed persistence. Run artifact storage is bounded by the pinned disk quota.

Metrics aggregate actual numeric provider input_tokens/output_tokens/cost, expose per-field known_count, and mark partial/unknown data rather than manufacturing zero cost. Public run `trace_url` is present only for configured loopback Langfuse and an actual persisted trace ID.

Manual integration resolution permits one operator continuation at attempt 2 on the original integration node, including when `max_retries` is 0. The original attempt must be a failed, stopped `INTEGRATION_CONFLICT` with `integration_status:needs_resolution`. A same-run `integration_resolution` approval binds the current graph, plan, base, policy, permitted paths, original execution and exact resolution tree; it must be approved, unexpired, and explicitly resumed. Dispatch includes `manual_resolution:true`, `target_execution_id`, `resolution_tree_sha`, and optional `resolution_approval_id` matching that approval. The ledger derives `resolution_approved`; transport cannot grant it. Deadline, cancellation, concurrency, general plan approval and accepted commit-set guards still apply. This exception does not enlarge provider, test, or other automatic retry budgets. Node attempt 2 above the automatic quota can only project an existing server-authorized continuation in the same run and node.

Scoped Codex MCP tool assignments use `permission:"read"` and an exact `mcp__<server>__<tool>` identifier. Components are case-sensitive ASCII names, start with a letter, contain at most 64 letters/digits/underscores/hyphens, and cannot contain `__`. Registry creation validates mapping format; agent creation/update, run pinning and dispatch additionally require the named server to be assigned and the tool to appear in its allowlist. Duplicate server names fail closed. MCP snapshots include the immutable config version's registry `name`, overridden only by an explicitly validated manifest `name`.

A supported read-only server registration is:

```json
{
  "name": "factoryfiles",
  "manifest": {
    "transport": "stdio",
    "argv": ["python3", "-m", "adf_runtime_worker.scoped_mcp"],
    "trusted": true,
    "allowed_tools": ["read_marker"],
    "scope": {
      "contract": "adf.scoped-files.v1",
      "root": "$WORKTREE",
      "read_paths": ["marker.txt"],
      "max_bytes": 4096
    },
    "timeout_sec": 2
  }
}
```

Assign its definition UUID in `mcp_server_ids` together with a tool definition whose manifest contains `input_schema`, `permission:"read"`, and `provider_mappings:{"codex":["mcp__factoryfiles__read_marker"]}`. The owned worker module supports `read_marker` and `read_file` only, using exact allocated-worktree paths; it independently enforces symlink and filesystem restrictions. `read_paths` contains 1–100 exact relative files without traversal, globs, credentials or agent configuration; case-insensitive components `.agents`, `.codex`, `.claude`, `.git`, `.ssh`, `.aws`, `.azure`, `.kube`, `.gnupg`, `.config` and private-key basenames `id_rsa`, `id_dsa`, `id_ecdsa`, `id_ed25519` are denied; `max_bytes` is a strict integer from 1–65536 (default 4096). Only the `python`/`python3 -m adf_runtime_worker.scoped_mcp` argv forms are registered, with no extra arguments, environment/auth maps or arbitrary server commands. Generic scope declarations cannot be assigned to Codex. `timeout_sec` remains bounded 1–1800 and the worker clamps it to the execution deadline. API validation of these declarations does not replace the worker's filesystem enforcement or live CLI acceptance gates.
