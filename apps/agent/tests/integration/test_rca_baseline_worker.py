"""Local protocol integration: no enterprise system or credentials are used."""

import json
import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('graph_id', 'scenario'),
    [
        *[
            ('rca.synthetic', scenario)
            for scenario in (
                'ok',
                'empty',
                'unavailable',
                'timeout',
                'crash',
                'scope_change',
                'revoked',
                'budget',
                'mixed_team',
                'grant_change',
                'grant_at_send',
                'grant_after_checkpoint',
                'legacy_receipt',
                'check_durable',
            )
        ],
        *[
            (graph_id, scenario)
            for graph_id in ('rca.proposal', 'rca.batch_proposal')
            for scenario in ('ok', 'empty', 'unavailable', 'scope_change', 'mixed_team')
        ],
    ],
)
async def test_synthetic_baseline_uses_one_scope_and_recovers_without_resend(
    platform, monkeypatch, graph_id, scenario
):
    import httpx
    from workbench_agent.domain.profiles import ProfileDraft, ProfileBudgetLimits
    from workbench_agent.domain.tools import ToolBinding
    from workbench_agent.jobs.worker import Worker, DeploymentConnectorPolicy
    from workbench_agent.application.synthetic_source_policy import SyntheticSourcePolicy
    from integration.test_synthetic_alert_v2 import event, ingress, signed
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry
    from workbench_agent.tools.permissions import DeploymentToolPermissions
    from workbench_agent.tools.policy import PolicyToolExecutor
    from workbench_agent.tools.registry import ToolRegistry
    from workbench_agent.tools.mcp import (
        MCPAdapter,
        ConnectionPolicy,
        TrustedConnection,
        StreamableSessionFactory,
    )
    from datetime import datetime, timedelta
    from support.mcp import mcp_server_source

    server_src = mcp_server_source()
    monkeypatch.syspath_prepend(str(server_src))
    from workbench_mcp_server.bootstrap import Runtime
    from workbench_mcp_server.infrastructure.config.settings import Settings
    from workbench_mcp_server.interfaces.mcp.server import create_http_app
    from workbench_mcp_server.application.results import GatewayError

    monkeypatch.setenv('M3_MCP_AGENT', 'fixture-agent')
    monkeypatch.setenv('M3_MCP_UPSTREAM', 'fixture-upstream')
    monkeypatch.setenv('M3_MCP_CURSOR', 'c' * 32)
    alert_event = event()
    end = datetime.fromisoformat(alert_event['anchor'])
    start = end - timedelta(seconds=alert_event['window_seconds'])
    sample = (start + timedelta(seconds=60)).isoformat()
    GRANT = {
        'kind': 'dod-mcp-v1',
        'team': 'ExampleTeam',
        'allowed_scope': {'service': 'checkout', 'environment': 'test', 'region': 'id'},
        'earliest': (start - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S'),
        'latest': (end + timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S'),
        'max_span_seconds': 3600,
        'timezone': 'UTC',
        'sources': {'metrics_query': 'metrics', 'logs_search': 'logs'},
    }
    names = ('metrics_query', 'logs_search')
    kinds = ('metrics', 'logs')
    scopes = [
        {
            'team': 'ExampleTeam',
            'environment': 'test',
            'region': 'id',
            'service': 'checkout',
            'source_id': kind,
        }
        for kind in kinds
    ]
    sources = []
    for name, kind, scope in zip(names, kinds, scopes):
        source = {
            'id': kind,
            'kind': kind,
            'adapter': 'normalized-http-v1',
            'endpoint': 'https://example.invalid/query',
            'auth_secret_ref': 'env://M3_MCP_UPSTREAM',
            'binding_id': 'fixture-' + kind,
            'scopes': [scope],
            'scope_enforced': True,
        }
        if kind == 'metrics':
            source['templates'] = {'error_rate': {}}
        sources.append(source)
    config = {
        'server': {'allowed_hosts': ['localhost:*']},
        'principals': [
            {
                'id': 'agent',
                'token_secret_ref': 'env://M3_MCP_AGENT',
                'tools': list(names),
                'scopes': scopes,
            }
        ],
        'sources': sources,
        'cursor_secret_ref': 'env://M3_MCP_CURSOR',
    }
    runtime = Runtime(Settings.model_validate(config))
    calls = []
    records = {
        'metrics': {
            'series': [
                {
                    'labels': {'service': 'checkout'},
                    'points': [[sample, 0.1]],
                    'unit': 'ratio',
                    'source_id': 'metric-1',
                }
            ]
        },
        'logs': {
            'entries': [
                {
                    'timestamp': sample,
                    'service': 'checkout',
                    'level': 'ERROR',
                    'message': 'sample',
                    'record_id': 'log-1',
                }
            ]
        },
    }

    class Gateway:
        def __init__(self, kind):
            self.kind = kind

        async def query(self, request):
            calls.append((self.kind, request))
            if self.kind == 'logs' and scenario in ('unavailable', 'timeout'):
                if scenario == 'timeout':
                    raise TimeoutError()
                raise GatewayError('unavailable', 'FIXTURE_UNAVAILABLE')
            if self.kind == 'logs' and scenario == 'empty':
                return {'entries': []}
            return records[self.kind]

    for kind in kinds:
        runtime.executor.gateways[kind] = Gateway(kind)
    app = create_http_app(runtime)
    lifespan = app.app.router.lifespan_context(app.app)
    await lifespan.__aenter__()
    try:
        factory = StreamableSessionFactory(
            {('local', 'read'): 'https://localhost:8000/mcp'},
            lambda *_: {'Authorization': 'Bearer fixture-agent'},
            transport=httpx.ASGITransport(app=app),
        )
        other_grant = dict(GRANT, team='OTHER')
        team = other_grant if scenario == 'mixed_team' else GRANT
        connection_rules = [
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
                (team,),
            ),
        ]
        adapter = MCPAdapter(factory, ConnectionPolicy(lambda: connection_rules))
        catalog = await adapter.discover(
            'local', 'read', team_id=platform.auth.team_id, agent_id=platform.agent_id
        )
        refs = []
        for name in names:
            schema = catalog.tools[catalog.names.index(name)].inputSchema
            binding = ToolBinding(
                alias='local.' + name,
                provider='mcp',
                tool_name=name,
                server='local',
                credential_scope='read',
                permission='baseline.read',
                input_schema=schema,
            )
            refs.append(
                await ToolRegistry(platform.backend).publish(
                    platform.auth, platform.agent_id, binding, key='tool-' + name
                )
            )
        context = await platform.contexts.resolve(platform.auth, platform.agent_id)
        ws = await platform.profiles.get_agent(platform.auth, platform.agent_id)
        draft = await platform.profiles.save_draft(
            platform.auth,
            platform.agent_id,
            ProfileDraft(
                default_model=context.default_model,
                graph_id=graph_id,
                tools=tuple(refs),
                budget_limits=ProfileBudgetLimits(tool_calls=1) if scenario == 'budget' else None,
            ),
            ws['revision'],
            key='baseline-draft',
        )
        await platform.profiles.publish(
            platform.auth, platform.agent_id, draft['revision'], key='baseline-profile'
        )
        identities = {
            'v2-test': {
                'subject': platform.auth.subject,
                'team_id': platform.auth.team_id,
                'agent_ids': [platform.agent_id],
            }
        }
        grant = dict(
            team_id=platform.auth.team_id,
            agent_id=platform.agent_id,
            connector_id='v2-test',
            identity_id='v2-test',
            subject=platform.auth.subject,
            service='checkout',
            environment='test',
            region='id',
            rule='error-rate',
        )
        await SyntheticSourcePolicy(platform.backend).sync(
            [grant], connector_identities=identities, expected_revision=0
        )
        run = await ingress(platform).accept(*signed(alert_event))
        rules = {
            'users': {platform.auth.subject: ['baseline.read']},
            'teams': {platform.auth.team_id: ['baseline.read']},
            'agents': {platform.agent_id: ['baseline.read']},
            'skills': {'__none__': ['baseline.read']},
            'experts': {'diagnosis': ['baseline.read']},
        }
        if scenario == 'revoked':
            rules['experts']['diagnosis'] = []
        platform.backend._tool_permissions = DeploymentToolPermissions(lambda: rules)
        platform.backend._rca_metric_template = 'error_rate'
        platform.backend._baseline_factory = lambda execution: PolicyToolExecutor(
            platform.backend, adapter, execution=execution, delays=(0, 0)
        )
        registry = foundation_worker_registry(platform.backend)
        policy = DeploymentConnectorPolicy(lambda: identities)
        registry.reconciler.connector_policy = policy
        worker = Worker(platform.backend, 'baseline-worker', registry, connector_policy=policy)
        if scenario == 'check_durable':
            original = PolicyToolExecutor.execute

            async def check_before_dispatch(self, *args, **kwargs):
                async with platform.backend.create_uow().begin_read() as uow:
                    checkpoint = await platform.backend.runs.checkpoint(
                        uow, platform.auth.team_id, run.run_id
                    )
                assert checkpoint['values']['baseline_authority'] == [
                    'ExampleTeam',
                    'test',
                    'id',
                    'checkout',
                    start.isoformat(),
                    end.isoformat(),
                ]
                return await original(self, *args, **kwargs)

            monkeypatch.setattr(PolicyToolExecutor, 'execute', check_before_dispatch)
        if scenario == 'grant_at_send':
            original_call = adapter.call

            async def changed_at_send(*args, **kwargs):
                if args[2] == 'logs_search':
                    connection_rules[1] = TrustedConnection(
                        platform.auth.team_id,
                        platform.agent_id,
                        'local',
                        'read',
                        frozenset({'logs_search'}),
                        (other_grant,),
                    )
                return await original_call(*args, **kwargs)

            monkeypatch.setattr(adapter, 'call', changed_at_send)
        if scenario == 'grant_change':
            original = PolicyToolExecutor.execute
            changed = False

            async def change_grant_after(self, *args, **kwargs):
                nonlocal changed
                result = await original(self, *args, **kwargs)
                if not changed:
                    changed = True
                    connection_rules[1] = TrustedConnection(
                        platform.auth.team_id,
                        platform.agent_id,
                        'local',
                        'read',
                        frozenset({'logs_search'}),
                        (other_grant,),
                    )
                return result

            monkeypatch.setattr(PolicyToolExecutor, 'execute', change_grant_after)
        if scenario == 'scope_change':
            original = PolicyToolExecutor.execute
            changed = False

            async def change_after(self, *args, **kwargs):
                nonlocal changed
                result = await original(self, *args, **kwargs)
                if not changed:
                    changed = True
                    from workbench_agent.application.context import ContextResolver

                    async with platform.backend.create_uow().begin_write() as uow:
                        await ContextResolver(platform.backend).query_scope(
                            uow,
                            platform.auth,
                            run.run_id,
                            {
                                'region': 'id',
                                'service': 'checkout',
                                'environment': 'test',
                                'start_at': (start + timedelta(seconds=60)).isoformat(),
                                'end_at': end.isoformat(),
                            },
                        )
                return result

            monkeypatch.setattr(PolicyToolExecutor, 'execute', change_after)
        if scenario in ('crash', 'grant_after_checkpoint', 'legacy_receipt'):
            original = PolicyToolExecutor.execute
            triggered = False

            async def crash_after(self, *args, **kwargs):
                nonlocal triggered
                result = await original(self, *args, **kwargs)
                if not triggered:
                    triggered = True
                    if scenario == 'legacy_receipt':
                        from sqlalchemy import update
                        from workbench_agent.persistence import schema as schema

                        async with platform.backend.create_uow().begin_write() as uow:
                            row = await platform.backend.tools.invocation(
                                uow, platform.auth.team_id, run.run_id, args[2]
                            )
                            receipt = json.loads(row['receipt_ref'])
                            receipt.pop('business_scope', None)
                            await uow.execute(
                                update(schema.tool_invocations)
                                .where(
                                    schema.tool_invocations.c.invocation_id == row['invocation_id']
                                )
                                .values(receipt_ref=json.dumps(receipt))
                            )
                    raise RuntimeError('simulated crash after receipt')
                return result

            monkeypatch.setattr(PolicyToolExecutor, 'execute', crash_after)
            assert await worker.run_once() == 'recovery_scheduled'
            monkeypatch.setattr(PolicyToolExecutor, 'execute', original)
            if scenario == 'grant_after_checkpoint':
                connection_rules[1] = TrustedConnection(
                    platform.auth.team_id,
                    platform.agent_id,
                    'local',
                    'read',
                    frozenset({'logs_search'}),
                    (other_grant,),
                )
            assert (await registry.reconciler.recover(run.run_id)).action == 'continue'
        assert await worker.run_once() == 'projected'
        view = await platform.get_run(run.run_id)
        assert view.status == 'partial'
        if scenario == 'legacy_receipt':
            assert len(calls) == 2 and len(view.outcome['evidence_refs']) == 1
            assert any(
                'metrics' in item and 'unknown' in item for item in view.outcome['unresolved_items']
            )
            async with platform.backend.create_uow().begin_read() as uow:
                visible = await platform.contexts.current_evidence(uow, platform.auth, run.run_id)
            assert len(visible) == 1
            return
        if scenario in (
            'scope_change',
            'budget',
            'revoked',
            'mixed_team',
            'grant_change',
            'grant_at_send',
            'grant_after_checkpoint',
        ):
            expected_calls = 0 if scenario in ('revoked', 'mixed_team') else 1
            assert len(calls) == expected_calls
            assert len(view.outcome['evidence_refs']) == (1 if scenario == 'budget' else 0)
            usage = await platform.budgets.snapshot(platform.auth, run.run_id)
            assert usage.tool_attempts == (2 if scenario == 'grant_at_send' else expected_calls)
            assert not any(f['status'] == 'normal' for f in view.outcome['findings'])
            if scenario in ('scope_change', 'mixed_team'):
                async with platform.backend.create_uow().begin_read() as uow:
                    saved = await platform.backend.runs.checkpoint(
                        uow, platform.auth.team_id, run.run_id
                    )
                expected = 'stale' if scenario == 'scope_change' else 'conflicting'
                assert expected in saved['values']['baseline_statuses'].values()
            return
        assert len(view.outcome['evidence_refs']) == (
            2 if scenario in ('ok', 'crash', 'check_durable') else 1
        )
        assert not any(f['status'] == 'normal' for f in view.outcome['findings'])
        if scenario in ('empty', 'unavailable', 'timeout'):
            assert any(
                'logs' in item and scenario in item for item in view.outcome['unresolved_items']
            )
        async with platform.backend.create_uow().begin_read() as uow:
            saved = await platform.backend.runs.checkpoint(uow, platform.auth.team_id, run.run_id)
            scope = await platform.backend.runs.current_scope(
                uow, platform.auth.team_id, run.run_id
            )
            evidence = await platform.backend.runs.current_evidence(
                uow, platform.auth.team_id, run.run_id
            )
            if scenario == 'ok':
                from uuid import UUID, uuid5

                invocations = await platform.backend.tools.succeeded_invocations(
                    uow, platform.auth.team_id, run.run_id
                )
                assert {row['logical_call_id'] for row in invocations} == {
                    str(uuid5(UUID(run.run_id), f'{graph_id}:baseline:{name}'))
                    for name in ('metrics', 'logs')
                }
        statuses = saved['values']['baseline_statuses']
        assert statuses['metrics'] == 'valid' and statuses['trace'] == 'unavailable'
        assert (
            statuses['logs']
            == {
                'ok': 'valid',
                'empty': 'empty',
                'unavailable': 'unavailable',
                'timeout': 'timeout',
                'crash': 'valid',
                'check_durable': 'valid',
            }[scenario]
        )
        assert len([kind for kind, _ in calls if kind == 'metrics']) == 1
        for kind, call in calls:
            assert call.context['query_scope_ref'] == scope['scope_id']
            assert (
                call.scope.service == 'checkout'
                and call.scope.region == 'id'
                and call.scope.environment == 'test'
            )
            from datetime import datetime

            assert datetime.fromisoformat(call.parameters['start']) == start
            assert datetime.fromisoformat(call.parameters['end']) == end
        for row in evidence:
            assert row['scope_id'] == scope['scope_id']
            assert json.loads(row['time_window']) == {
                'start_at': start.isoformat(),
                'end_at': end.isoformat(),
            }
        async with platform.backend.create_uow().begin_read() as uow:
            disclosed = await platform.contexts.current_evidence(uow, platform.auth, run.run_id)
        assert len(disclosed) == len(evidence) and len(disclosed) >= 1
        rules['experts']['diagnosis'] = []
        async with platform.backend.create_uow().begin_read() as uow:
            assert await platform.contexts.current_evidence(uow, platform.auth, run.run_id) == []
        usage = await platform.budgets.snapshot(platform.auth, run.run_id)
        assert usage.tool_attempts == len(calls)
    finally:
        await lifespan.__aexit__(None, None, None)
        await runtime.close()
