"""Local MCP service and synthetic data only; no business access."""

from pathlib import Path
import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'scenario',
    ['mixed', 'same', 'grant_change', 'grant_at_send', 'grant_after_checkpoint', 'legacy_receipt'],
)
@pytest.mark.parametrize('graph_id', ['sop.conditional'])
async def test_sop_rejects_mixed_business_teams_before_dispatch(
    platform, monkeypatch, scenario, graph_id
):
    import httpx
    from workbench_agent.application.runs import RunService
    from workbench_agent.application.workflows import WorkflowService
    from workbench_agent.domain.workflows import WorkflowDefinition
    from workbench_agent.domain.profiles import ProfileDraft
    from workbench_agent.domain.tools import ToolBinding
    from workbench_agent.domain.runs import CreateRun
    from workbench_agent.tools.registry import ToolRegistry
    from workbench_agent.tools.permissions import DeploymentToolPermissions
    from workbench_agent.tools.policy import PolicyToolExecutor
    from workbench_agent.tools.mcp import (
        MCPAdapter,
        ConnectionPolicy,
        TrustedConnection,
        StreamableSessionFactory,
    )
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry
    from workbench_agent.jobs.worker import Worker
    from contract.test_scoped_mcp_mapping import GRANT
    from support.mcp import mcp_server_source

    source = mcp_server_source()
    monkeypatch.syspath_prepend(str(source))
    from workbench_mcp_server.bootstrap import Runtime
    from workbench_mcp_server.infrastructure.config.settings import Settings
    from workbench_mcp_server.interfaces.mcp.server import create_http_app

    monkeypatch.setenv('T18_MCP_AGENT', 'fictional')
    monkeypatch.setenv('T18_MCP_SOURCE', 'fictional')
    scope = {
        'team': 'ExampleTeam',
        'environment': 'test',
        'region': 'id',
        'service': 'checkout',
        'source_id': 'metrics',
    }
    scope2 = dict(scope, team='OTHER' if scenario == 'mixed' else 'ExampleTeam', source_id='logs')
    sources = [
        {
            'id': 'metrics',
            'kind': 'metrics',
            'adapter': 'normalized-http-v1',
            'endpoint': 'https://example.invalid/query',
            'auth_secret_ref': 'env://T18_MCP_SOURCE',
            'binding_id': 'm',
            'scopes': [scope],
            'scope_enforced': True,
            'templates': {'error_rate': {}},
        },
        {
            'id': 'logs',
            'kind': 'logs',
            'adapter': 'normalized-http-v1',
            'endpoint': 'https://example.invalid/query',
            'auth_secret_ref': 'env://T18_MCP_SOURCE',
            'binding_id': 'l',
            'scopes': [scope2],
            'scope_enforced': True,
        },
    ]
    monkeypatch.setenv('T18_CURSOR', 'c' * 32)
    settings = Settings.model_validate(
        {
            'server': {'allowed_hosts': ['localhost:*']},
            'principals': [
                {
                    'id': 'agent',
                    'token_secret_ref': 'env://T18_MCP_AGENT',
                    'tools': ['metrics_query', 'logs_search'],
                    'scopes': [scope, scope2],
                }
            ],
            'sources': sources,
            'cursor_secret_ref': 'env://T18_CURSOR',
        }
    )
    runtime = Runtime(settings)
    calls = []

    class Gateway:
        async def query(self, request):
            calls.append(request)
            if request.tool == 'metrics_query':
                return {
                    'series': [
                        {
                            'labels': {'service': 'checkout'},
                            'points': [['2026-11-13T00:01:00Z', 0.1]],
                            'unit': 'ratio',
                            'source_id': 'm',
                        }
                    ]
                }
            return {
                'entries': [
                    {
                        'timestamp': '2026-11-13T00:01:00Z',
                        'service': 'checkout',
                        'level': 'ERROR',
                        'message': 'example',
                        'record_id': 'l',
                    }
                ]
            }

    runtime.executor.gateways['metrics'] = Gateway()
    runtime.executor.gateways['logs'] = Gateway()
    app = create_http_app(runtime)
    life = app.app.router.lifespan_context(app.app)
    await life.__aenter__()
    try:
        factory = StreamableSessionFactory(
            {('local', 'read'): 'https://localhost:8000/mcp'},
            lambda *_: {'Authorization': 'Bearer fictional'},
            transport=httpx.ASGITransport(app=app),
        )
        rules = [
            TrustedConnection(
                platform.auth.team_id,
                platform.agent_id,
                'local',
                'read',
                frozenset({'metrics_query'}),
                (GRANT,),
            ),
            TrustedConnection(
                platform.auth.team_id,
                platform.agent_id,
                'local',
                'read',
                frozenset({'logs_search'}),
                (dict(GRANT, team='OTHER') if scenario == 'mixed' else GRANT,),
            ),
        ]
        adapter = MCPAdapter(factory, ConnectionPolicy(lambda: rules))
        catalog = await adapter.discover(
            'local', 'read', team_id=platform.auth.team_id, agent_id=platform.agent_id
        )
        refs = []
        for name in ('metrics_query', 'logs_search'):
            schema = catalog.tools[catalog.names.index(name)].inputSchema
            refs.append(
                await ToolRegistry(platform.backend).publish(
                    platform.auth,
                    platform.agent_id,
                    ToolBinding(
                        alias=name,
                        provider='mcp',
                        tool_name=name,
                        server='local',
                        credential_scope='read',
                        permission='read',
                        input_schema=schema,
                    ),
                    key=name,
                )
            )
        context = await platform.contexts.resolve(platform.auth, platform.agent_id)
        ws = await platform.profiles.get_agent(platform.auth, platform.agent_id)
        draft = await platform.profiles.save_draft(
            platform.auth,
            platform.agent_id,
            ProfileDraft(default_model=context.default_model, graph_id=graph_id, tools=tuple(refs)),
            ws['revision'],
            key='mixed-draft',
        )
        await platform.profiles.publish(
            platform.auth, platform.agent_id, draft['revision'], key='mixed-publish'
        )
        wf = await WorkflowService(platform.backend).publish(
            platform.auth,
            platform.agent_id,
            WorkflowDefinition.model_validate(
                {
                    'name': 'check',
                    'steps': [
                        {
                            'step_id': 'metrics',
                            'tool': 'metrics_query',
                            'arguments': {'template_id': 'error_rate'},
                        },
                        {
                            'step_id': 'logs',
                            'tool': 'logs_search',
                            'depends_on': ['metrics'],
                            'inputs': {'keyword': 'metrics.schema_version'},
                        },
                    ],
                }
            ),
            key='mixed-wf',
        )
        request = CreateRun.model_validate(
            {
                'agent_id': platform.agent_id,
                'mode': 'sop',
                'workflow_version_id': wf.workflow_version_id,
                'input': {'text': 'check'},
                'summary_token_reserve': 8000,
            }
        )
        run = await RunService(platform.backend).create(platform.auth, request, 'mixed-run')
        async with platform.backend.create_uow().begin_write() as uow:
            await platform.contexts.query_scope(
                uow,
                platform.auth,
                run.run_id,
                {
                    'region': 'id',
                    'service': 'checkout',
                    'environment': 'test',
                    'start_at': '2026-11-13T00:00:00+00:00',
                    'end_at': '2026-11-13T00:10:00+00:00',
                },
            )
        perms = {
            'users': {platform.auth.subject: ['read']},
            'teams': {platform.auth.team_id: ['read']},
            'agents': {platform.agent_id: ['read']},
            'skills': {'__none__': ['read']},
            'experts': {'diagnosis': ['read']},
        }
        platform.backend._tool_permissions = DeploymentToolPermissions(lambda: perms)
        platform.backend._sop_factory = lambda execution: PolicyToolExecutor(
            platform.backend, adapter, execution=execution, delays=(0, 0)
        )
        registry = foundation_worker_registry(platform.backend)
        worker = Worker(platform.backend, 'sop-mixed', registry)
        other = TrustedConnection(
            platform.auth.team_id,
            platform.agent_id,
            'local',
            'read',
            frozenset({'logs_search'}),
            (dict(GRANT, team='OTHER'),),
        )
        if scenario == 'grant_change':
            original = PolicyToolExecutor.execute

            async def change_after(self, *args, **kwargs):
                result = await original(self, *args, **kwargs)
                rules[1] = other
                return result

            monkeypatch.setattr(PolicyToolExecutor, 'execute', change_after)
        if scenario == 'grant_at_send':
            original_call = adapter.call

            async def change_at_send(*args, **kwargs):
                if args[2] == 'logs_search':
                    rules[1] = other
                return await original_call(*args, **kwargs)

            monkeypatch.setattr(adapter, 'call', change_at_send)
        if scenario in ('grant_after_checkpoint', 'legacy_receipt'):
            original = PolicyToolExecutor.execute

            async def crash_after(self, *args, **kwargs):
                _result = await original(self, *args, **kwargs)
                if scenario == 'legacy_receipt':
                    import json
                    from sqlalchemy import update
                    from workbench_agent.persistence import schema

                    async with platform.backend.create_uow().begin_write() as uow:
                        row = await platform.backend.tools.invocation(
                            uow, platform.auth.team_id, run.run_id, args[2]
                        )
                        receipt = json.loads(row['receipt_ref'])
                        receipt.pop('business_scope', None)
                        await uow.execute(
                            update(schema.tool_invocations)
                            .where(schema.tool_invocations.c.invocation_id == row['invocation_id'])
                            .values(receipt_ref=json.dumps(receipt))
                        )
                raise RuntimeError('crash after receipt')

            monkeypatch.setattr(PolicyToolExecutor, 'execute', crash_after)
            assert await worker.run_once() == 'recovery_scheduled'
            monkeypatch.setattr(PolicyToolExecutor, 'execute', original)
            if scenario == 'grant_after_checkpoint':
                rules[1] = other
            assert (await registry.reconciler.recover(run.run_id)).action == 'continue'
        assert await worker.run_once() == 'projected'
        view = await platform.get_run(run.run_id)
        assert view.status == 'partial'
        assert (
            len(calls)
            == {
                'mixed': 0,
                'same': 2,
                'grant_change': 1,
                'grant_at_send': 1,
                'grant_after_checkpoint': 1,
                'legacy_receipt': 1,
            }[scenario]
        )
        if scenario == 'same':
            assert calls[0].tool == 'metrics_query'
            assert calls[1].tool == 'logs_search'
            assert calls[1].parameters['keyword'] == 'dod-tool-result-v1'
    finally:
        await life.__aexit__(None, None, None)
        await runtime.close()
