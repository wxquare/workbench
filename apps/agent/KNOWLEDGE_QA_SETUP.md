---
title: "Knowledge QA: Agent configuration and scoped evidence"
---

This runbook uses the current Agent baseline: `schema_agent_v1.py`, the separate
`agent_migrations` chain at `0001_agent_domain`, and storage protocol
`dod-storage-agent-v1`. Follow [local startup](../../docs/runbooks/source-layout.md)
to explicitly select and initialize a fresh SQLite path. Keep existing databases
and their WAL/SHM files intact; the old Workspace schema must fail migration and
readiness safely. Backend mechanical renaming is complete; remaining changes and
regression verification are tracked in [specification 21.4](../../docs/specs/2026-11-12-workbench-python-spec.md#214-移除-workspace-的-agent-领域重构).

`dod.v16` is a separate, immutable QA graph. Bootstrap keeps its existing
`dod.v3` profile and never switches an existing agent. An administrator
must explicitly publish a knowledge tool and then a new agent profile.
Existing runs remain pinned to their original profile and graph. The new
profile serves QA only; use a separate agent if other modes are needed.

The product 我的工作台 hosts multiple independently configured Agents; DoD Agent
is one scenario. There is no Workspace product or domain layer. Creating an Agent
uses a published team-scoped model and new draft defaults, independently of any
existing active Agent; it does not inherit another Agent's system prompt,
behavior, budgets, history settings, resources, or credential grants. Creation
stores a team-level idempotency receipt. An identity restricted by
`auth.agent_scope` cannot create a new Agent outside that scope. Initialization
behavior and regression tests remain in progress; configure and publish the
new Agent's QA profile and deployment grants explicitly.

## Deployment configuration

Configure the API, independent worker, and scheduler against the same migrated
SQLite database and shared local persistent root. All need the same
`service.tool_permissions` because the API rechecks evidence visibility on
reads and the scheduler rechecks it during recovery. The worker additionally needs `model_credential_bindings` and
`mcp_connections`. Environment references must be available to the processes
that use them. Use real deployment values and secrets supplied by your secret
management system; the following is a configuration shape, not working
credentials:

```yaml
service:
  model_credential_bindings:
    - team_id: "<team UUID>"
      agent_ids: ["<agent UUID>"]
      base_url: "<published model HTTPS base URL>"
      secret_ref: "env://MODEL_SECRET"
  mcp_connections:
    - team_id: "<team UUID>"
      agent_id: "<agent UUID>"
      server: "knowledge-server"
      credential_scope: "knowledge-reader"
      endpoint: "<DoD MCP HTTPS endpoint>"
      secret_ref: "env://MCP_SECRET"
      read_tools: [knowledge_search]
      scope_grants:
        - kind: dod-mcp-v1
          team: "<business team>"
          allowed_scope: {service: "<service>", environment: "<environment>", region: "<region>"}
          earliest: "2026-01-01 00:00:00"
          latest: "2027-01-01 00:00:00"
          max_span_seconds: 3600
          timezone: "Asia/Shanghai"
          sources: {knowledge_search: "<authorized MCP source_id>"}
  tool_permissions:
    users: {"<authenticated subject>": [knowledge.read]}
    teams: {"<team UUID>": [knowledge.read]}
    agents: {"<agent UUID>": [knowledge.read]}
    skills: {"__none__": [knowledge.read]}
    experts: {knowledge: [knowledge.read]}
```

The published model's endpoint and secret reference must match its credential
binding. Add permissions for each pinned skill instead of `__none__` when the
profile has skills: the key is `resource_id:version:content_hash`. The MCP
principal and upstream source must independently authorize the same business
scope. The grant, not the caller or model, supplies business `team` and
`source_id`. Set the time bounds, timezone, and maximum span to the actual
authorized interval. Restart API, worker, and scheduler after changing process settings.
The current worker registry advertises its registered graphs; the
`worker.supported_graph_versions` settings field is not a capability filter.

## Publish into an existing agent

These commands use the authenticated API and require `curl`, `jq`, the agent
environment, and an administrator bearer token. Start API and worker with the
configured deployment first. Set the following environment variables to your
values; `WORKBENCH_CONFIG` points to the same config used by the worker. Use a new
tool alias to avoid modifying another draft. The `PATCH` below replaces the
agent **draft**, so inspect it and coordinate with its owner before
proceeding. Publishing creates a new version and changes the active profile;
it does not modify earlier published versions.

```sh
export WORKBENCH_CONFIG=/absolute/path/to/agent.yaml
export WORKBENCH_API=http://127.0.0.1:8000
export WORKBENCH_TEAM_ID='<team UUID>'
export WORKBENCH_AGENT_ID='<agent UUID>'
export WORKBENCH_BEARER='<administrator bearer token>'
export WORKBENCH_SERVER=knowledge-server WORKBENCH_CREDENTIAL_SCOPE=knowledge-reader
export WORKBENCH_TOOL_ALIAS=qa-knowledge-search
export WORKBENCH_AGENT_PYTHON=/absolute/path/to/apps/agent/.venv/bin/python
qa_tmp=$(mktemp -d)
```

Discover the exact schema from the configured server rather than inventing
one. The server and credential scope must match the deployment binding.

```sh
"$WORKBENCH_AGENT_PYTHON" - "$WORKBENCH_CONFIG" "$WORKBENCH_TEAM_ID" "$WORKBENCH_AGENT_ID" "$WORKBENCH_SERVER" "$WORKBENCH_CREDENTIAL_SCOPE" > "$qa_tmp/schema.json" <<'PY'
import asyncio, json, sys
from workbench_agent.connectors.secrets import SecretProvider
from workbench_agent.settings import load_settings
from workbench_agent.tools.mcp import configured_adapter

async def main():
    config, team, agent, server, credential_scope = sys.argv[1:]
    adapter = configured_adapter(load_settings(config).service.mcp_connections, SecretProvider())
    catalog = await adapter.discover(server, credential_scope, team_id=team, agent_id=agent)
    tool = catalog.tools[catalog.names.index('knowledge_search')]
    print(json.dumps(tool.inputSchema))

asyncio.run(main())
PY
```

Create and publish the tool. A new alias starts at draft revision zero; if it
already exists, `If-Match: 0` fails instead of overwriting it.

```sh
jq -n --arg alias "$WORKBENCH_TOOL_ALIAS" --arg server "$WORKBENCH_SERVER" --arg scope "$WORKBENCH_CREDENTIAL_SCOPE" \
  --slurpfile schema "$qa_tmp/schema.json" \
  '{alias:$alias,provider:"mcp",tool_name:"knowledge_search",server:$server,credential_scope:$scope,permission:"knowledge.read",input_schema:$schema[0],read_only:true}' > "$qa_tmp/tool.json"
curl -fsS -X PUT "$WORKBENCH_API/v1/agents/$WORKBENCH_AGENT_ID/tool-bindings/$WORKBENCH_TOOL_ALIAS" \
  -H "Authorization: Bearer $WORKBENCH_BEARER" -H 'Content-Type: application/json' \
  -H 'If-Match: 0' -H "Idempotency-Key: $(uuidgen)" --data-binary @"$qa_tmp/tool.json"
curl -fsS -X POST "$WORKBENCH_API/v1/agents/$WORKBENCH_AGENT_ID/tool-bindings/$WORKBENCH_TOOL_ALIAS/publish" \
  -H "Authorization: Bearer $WORKBENCH_BEARER" -H 'If-Match: 1' \
  -H "Idempotency-Key: $(uuidgen)" > "$qa_tmp/tool-ref.json"
```

Review the existing draft and the new draft before publishing. Preserve its
model, skill, repository, and other tool references only if they are intended
for this QA agent. Each referenced model must have its secret configured.

```sh
curl -fsS "$WORKBENCH_API/v1/agents/$WORKBENCH_AGENT_ID" -H "Authorization: Bearer $WORKBENCH_BEARER" > "$qa_tmp/agent.json"
jq '.draft' "$qa_tmp/agent.json"
jq --slurpfile ref "$qa_tmp/tool-ref.json" '.draft | .graph_version="dod.v16" | .allowed_modes=["qa"] | .tools += $ref' "$qa_tmp/agent.json" > "$qa_tmp/qa-draft.json"
jq . "$qa_tmp/qa-draft.json"
curl -fsS -X PATCH "$WORKBENCH_API/v1/agents/$WORKBENCH_AGENT_ID" \
  -H "Authorization: Bearer $WORKBENCH_BEARER" -H 'Content-Type: application/json' \
  -H "If-Match: $(jq -r .revision "$qa_tmp/agent.json")" \
  -H "Idempotency-Key: $(uuidgen)" --data-binary @"$qa_tmp/qa-draft.json" > "$qa_tmp/saved.json"
curl -fsS -X POST "$WORKBENCH_API/v1/agents/$WORKBENCH_AGENT_ID/profiles/publish" \
  -H "Authorization: Bearer $WORKBENCH_BEARER" -H "If-Match: $(jq -r .revision "$qa_tmp/saved.json")" \
  -H "Idempotency-Key: $(uuidgen)"
```

The optional tool probe is `POST /v1/agents/{agent_id}/tool-bindings/{alias}/probe`;
poll the returned job at `GET /v1/agents/{agent_id}/admin-jobs/{job_id}`.
Its schema must match. A business query probe is only run when a
deployment-owned `probe_requests` entry exists.

## API contract

Send `POST /v1/runs` with `Authorization: Bearer ...`, `Idempotency-Key`, and:

```json
{"agent_id":"<agent UUID>","mode":"qa","input":{"text":"What should I do?"},"business_context":{"query_scope":{"service":"checkout","environment":"test","region":"id","start_at":"2026-11-13T00:00:00Z","end_at":"2026-11-13T00:10:00Z"}}}
```

The five scope keys are required with no extras. Values are nonempty trimmed
strings; timestamps must both be timezone-aware ISO 8601, or both use
`YYYY-MM-DD HH:MM:SS` in the grant's timezone. The end must be later, with a
maximum span of one hour. Admission returns 202 and commits the scope with
the run; invalid input returns 422 and non-QA mode returns 409. Admission does
not grant access: deployment scope and current user/tool permissions are
checked when the worker uses the tool and when evidence is disclosed.

Poll `GET /v1/runs/{run_id}`. The result is `partial` and contains exact,
validated source quotations in `outcome.findings`, linked to
`outcome.evidence_refs`; it does not claim the model's free text as fact.
Missing, denied, truncated, invalid, or revoked evidence cannot yield a cited
answer. Retrieve readable evidence with
`GET /v1/runs/{run_id}/evidence/{evidence_id}`.

## Isolated browser fixture

From `apps/agent`, run:

```sh
.venv/bin/python tests/support/qa_browser_harness.py --port 8765
```

The harness uses a new temporary database, random synthetic credentials,
synthetic model and MCP source, and a separate worker process. It prints the
local `/app` and `/docs` URLs, development bearer token, agent UUID, and
authorized scope. `/fixture` exposes the same fixture values locally. The
default frontend directory is `apps/web/dist`; pass `--frontend-dist` to use a
different existing build. Use the printed token and scope in the UI. The
database is deleted when the harness exits. No live database or upstream is
used. The HTTP smoke test is `pytest -q tests/integration/test_qa_browser_harness.py`
and requires permission to bind loopback sockets.
