import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from support.runs import make_run_payload
from test_scope_mapping_policy import SCOPE, connection, mapping
from workbench_agent.application.runs import RunService
from workbench_agent.domain.profiles import ProfileDraft
from workbench_agent.domain.tools import ToolBinding
from workbench_agent.jobs.worker import Worker
from workbench_agent.runtime.graphs.ids import GraphId
from workbench_agent.runtime.graphs.registry import foundation_worker_registry
from workbench_agent.runtime.models.invoker import ModelInvoker
from workbench_agent.tools.mcp import (
    ConnectionPolicy,
    MCPAdapter,
    TrustedConnection,
)
from workbench_agent.tools.permissions import DeploymentToolPermissions
from workbench_agent.tools.policy import PolicyToolExecutor
from workbench_agent.tools.registry import ToolRegistry


FULL_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'properties': {
        'query': {'type': 'string'},
        'scope': {
            'type': 'object',
            'additionalProperties': False,
            'properties': {
                'team': {'type': 'string'},
                'environment': {'type': 'string'},
                'region': {'type': 'string'},
                'service': {'type': 'string'},
                'source_id': {'type': 'string'},
            },
            'required': ['team', 'environment', 'region', 'service', 'source_id'],
        },
        'context': {
            'type': 'object',
            'additionalProperties': False,
            'properties': {
                'run_id': {'type': 'string'},
                'invocation_id': {'type': 'string'},
                'query_scope_ref': {'type': 'string'},
            },
            'required': ['run_id', 'invocation_id', 'query_scope_ref'],
        },
    },
    'required': ['query', 'scope', 'context'],
}


class Session:
    def __init__(self, seen):
        self.seen = seen

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def list_tools(self):
        return SimpleNamespace(
            tools=[
                {
                    'name': 'test_read_counter',
                    'inputSchema': FULL_SCHEMA,
                }
            ],
            nextCursor=None,
        )

    async def call_tool(self, name, args):
        assert name == 'test_read_counter'
        self.seen.append(json.loads(json.dumps(args)))
        return SimpleNamespace(
            isError=False,
            structuredContent={'answer': 'counter=7'},
        )


async def setup(platform, client):
    seen = []
    deployment = connection(platform)
    adapter = MCPAdapter(
        lambda *_: Session(seen),
        ConnectionPolicy(
            [
                TrustedConnection(
                    platform.auth.team_id,
                    platform.agent_id,
                    'local',
                    'read',
                    frozenset({'test_read_counter'}),
                    (grant(),),
                    (deployment.mapping_policies[0].model_dump(mode='json'),),
                    frozenset({'test_read_counter'}),
                )
            ]
        ),
    )
    registry = ToolRegistry(
        platform.backend,
        mcp_connections=(deployment,),
    )
    binding = ToolBinding(
        alias='counter.read',
        provider='mcp',
        tool_name='test_read_counter',
        server='local',
        credential_scope='read',
        permission='counter.read',
        input_schema=FULL_SCHEMA,
        output_schema={
            'type': 'object',
            'additionalProperties': False,
            'properties': {'answer': {'type': 'string'}},
            'required': ['answer'],
        },
        argument_mapping=mapping(),
    )
    ref = await registry.publish(
        platform.auth,
        platform.agent_id,
        binding,
        key='configured-mapped-tool',
        managed=True,
    )
    platform.backend._mcp_connections = (deployment,)
    context = await platform.contexts.resolve(platform.auth, platform.agent_id)
    agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    saved = await platform.profiles.save_draft(
        platform.auth,
        platform.agent_id,
        ProfileDraft(
            default_model=context.default_model,
            graph_id=GraphId.CONVERSATION_TOOL_USE,
            allowed_modes=('qa',),
            tools=(ref,),
        ),
        agent['revision'],
        key='configured-mapped-profile',
    )
    await platform.profiles.publish(
        platform.auth,
        platform.agent_id,
        saved['revision'],
        key='configured-mapped-profile-publish',
    )
    payload = make_run_payload(platform.agent_id)
    payload['business_context'] = {
        'conversation': True,
        'query_scope': SCOPE,
    }
    response = await client.post(
        '/v1/runs',
        json=payload,
        headers={'Idempotency-Key': 'configured-mapped-run'},
    )
    assert response.status_code == 202, response.text
    return adapter, seen, response.json()['run_id']


def grant():
    return {
        'kind': 'dod-mcp-v1',
        'team': 'ExampleTeam',
        'allowed_scope': {
            'service': SCOPE['service'],
            'environment': SCOPE['environment'],
            'region': SCOPE['region'],
        },
        'earliest': '2026-11-20 00:00:00',
        'latest': '2026-11-20 23:59:59',
        'max_span_seconds': 3600,
        'timezone': 'Asia/Shanghai',
        'sources': {
            'knowledge_search': 'counter',
            'test_read_counter': 'counter',
        },
    }


class MappedModel:
    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages, **kwargs):
        usage = {
            'input_tokens': 4,
            'output_tokens': 2,
            'total_tokens': 6,
        }
        if any(isinstance(message, ToolMessage) for message in messages):
            return AIMessage(content='counter=7', usage_metadata=usage)
        return AIMessage(
            content='',
            tool_calls=[
                {
                    'name': 'counter_read',
                    'args': {'query': 'current'},
                    'id': 'configured-call',
                    'type': 'tool_call',
                }
            ],
            usage_metadata=usage,
        )


@pytest.mark.asyncio
async def test_unlisted_tool_name_runs_from_configuration_and_evidence_read(platform, client):
    permissions = DeploymentToolPermissions(
        lambda: {
            'users': {platform.auth.subject: ['counter.read']},
            'teams': {platform.auth.team_id: ['counter.read']},
            'agents': {platform.agent_id: ['counter.read']},
            'skills': {'__none__': ['counter.read']},
            'experts': {'knowledge': ['counter.read']},
        }
    )
    adapter, seen, run_id = await setup(platform, client)

    class Factory:
        def build(self, *args, **kwargs):
            return MappedModel()

    platform.backend._tool_permissions = permissions
    platform.backend._mcp_connection_policy = adapter.policy
    platform.backend._diagnosis_factory = lambda execution: (
        ModelInvoker(platform.backend, Factory(), object(), execution=execution),
        PolicyToolExecutor(
            platform.backend,
            adapter,
            execution=execution,
            delays=(0, 0),
        ),
    )
    worker = Worker(
        platform.backend,
        'mapped-tool-worker',
        foundation_worker_registry(
            platform.backend,
            supported_graph_ids=['conversation.tool_use'],
        ),
    )

    assert await worker.run_once() == 'projected'
    view = (await RunService(platform.backend).get(platform.auth, run_id)).model_dump(mode='json')
    assert view['status'] == 'succeeded'
    assert 'counter=7' in view['outcome']['summary']
    assert len(seen) == 1
    assert seen[0]['scope']['team'] == 'ExampleTeam'
    assert seen[0]['context']['query_scope_ref']
    async with platform.backend.create_uow().begin_read() as uow:
        payload = await platform.backend.tools.evidence_payload(
            uow,
            platform.auth.team_id,
            run_id,
            view['outcome']['evidence_refs'][0],
        )
    content = json.loads(payload['payload'])
    evidence_id = view['outcome']['evidence_refs'][0]
    evidence = await client.get(f'/v1/runs/{run_id}/evidence/{evidence_id}')
    assert content == {'answer': 'counter=7'}
    assert evidence.status_code == 200
    assert 'counter=7' in evidence.text

    platform.backend._mcp_connections = ()
    platform.backend._mcp_connection_policy = ConnectionPolicy([])
    denied = await client.get(f'/v1/runs/{run_id}/evidence/{evidence_id}')
    assert denied.status_code == 404
