import pytest
from pydantic import ValidationError

from workbench_agent.domain.workflows import WorkflowDefinition
from support.workflows import create_workflow_version


def definition(**overrides):
    values = {
        'name': 'payment-check',
        'steps': [
            {'step_id': 'metrics', 'tool': 'metrics.read', 'required': True},
            {
                'step_id': 'logs',
                'tool': 'logs.read',
                'depends_on': ['metrics'],
                'inputs': {'service': 'metrics.service'},
            },
        ],
    }
    values.update(overrides)
    return WorkflowDefinition.model_validate(values)


def test_workflow_rejects_cycles_unknown_edges_and_duplicate_ids():
    for steps in [
        [
            {'step_id': 'a', 'tool': 'x', 'depends_on': ['b']},
            {'step_id': 'b', 'tool': 'x', 'depends_on': ['a']},
        ],
        [{'step_id': 'a', 'tool': 'x', 'depends_on': ['missing']}],
        [{'step_id': 'a', 'tool': 'x'}, {'step_id': 'a', 'tool': 'y'}],
    ]:
        with pytest.raises(ValidationError):
            definition(steps=steps)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'scenario',
    [
        'timeout',
        'crash',
        'step_timeout',
        'success_crash',
        'scope_change',
        'revoked',
        'revoked_at_dispatch',
        'optional_failure',
    ],
)
async def test_worker_runs_pinned_workflow_and_required_failure_remains_partial(
    platform, monkeypatch, scenario
):
    from workbench_agent.domain.profiles import ProfileDraft
    from workbench_agent.domain.runs import CreateRun
    from workbench_agent.domain.tools import ToolBinding
    from workbench_agent.application.runs import RunService
    from workbench_agent.tools.registry import ToolRegistry
    from workbench_agent.tools.permissions import DeploymentToolPermissions
    from workbench_agent.tools.policy import PolicyToolExecutor
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry
    from workbench_agent.jobs.worker import Worker

    binding = ToolBinding(
        alias='metrics.read',
        provider='mcp',
        tool_name='local.metrics',
        server='local',
        credential_scope='read',
        permission='metrics.read',
        input_schema={
            'type': 'object',
            'properties': {'service': {'type': 'string'}},
            'required': ['service'],
            'additionalProperties': False,
        },
    )
    ref = await ToolRegistry(platform.backend).publish(
        platform.auth, platform.agent_id, binding, key='sop-tool'
    )
    other_ref = None
    if scenario == 'revoked_at_dispatch':
        other_binding = binding.model_copy(
            update={'alias': 'other.read', 'permission': 'other.read'}
        )
        other_ref = await ToolRegistry(platform.backend).publish(
            platform.auth, platform.agent_id, other_binding, key='other-tool'
        )
    context = await platform.contexts.resolve(platform.auth, platform.agent_id)
    ws = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    draft = await platform.profiles.save_draft(
        platform.auth,
        platform.agent_id,
        ProfileDraft(
            default_model=context.default_model,
            graph_id='sop.conditional',
            tools=(ref,) if other_ref is None else (ref, other_ref),
        ),
        ws['revision'],
        key='sop-draft',
    )
    await platform.profiles.publish(
        platform.auth, platform.agent_id, draft['revision'], key='sop-profile'
    )
    steps = [
        {
            'step_id': 'metrics',
            'tool': 'metrics.read',
            'required': scenario != 'optional_failure',
            'timeout_seconds': 1 if scenario == 'step_timeout' else 20,
            'arguments': {'service': 'checkout'},
        }
    ]
    if scenario in ('scope_change', 'revoked', 'revoked_at_dispatch', 'optional_failure'):
        steps.append(
            {
                'step_id': 'logs',
                'tool': 'other.read' if other_ref else 'metrics.read',
                'required': True,
                'depends_on': ['metrics'],
                'arguments': {'service': 'checkout'},
            }
        )
    wf = await create_workflow_version(platform, definition(steps=steps))
    request = CreateRun.model_validate(
        {
            'agent_id': platform.agent_id,
            'mode': 'sop',
            'workflow_version_id': wf.workflow_version_id,
            'input': {'text': 'Check'},
            'summary_token_reserve': 8000,
        }
    )
    run = await RunService(platform.backend).create(platform.auth, request, 'sop-run')
    newer = await create_workflow_version(
        platform, definition(steps=[{'step_id': 'other', 'tool': 'different'}])
    )
    assert newer.version == 2
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.contexts.query_scope(uow, platform.auth, run.run_id, {'service': 'checkout'})
    rules = {
        'users': {platform.auth.subject: ['metrics.read']},
        'teams': {platform.auth.team_id: ['metrics.read']},
        'agents': {platform.agent_id: ['metrics.read']},
        'skills': {'__none__': ['metrics.read']},
        'experts': {'diagnosis': ['metrics.read', 'other.read']},
    }
    if other_ref:
        for layer, name in [
            ('users', platform.auth.subject),
            ('teams', platform.auth.team_id),
            ('agents', platform.agent_id),
            ('skills', '__none__'),
        ]:
            rules[layer][name].append('other.read')
    platform.backend._tool_permissions = DeploymentToolPermissions(lambda: rules)

    class Transport:
        calls = 0

        async def call(self, *args, **kwargs):
            self.calls += 1
            if scenario in ('success_crash', 'scope_change', 'revoked', 'revoked_at_dispatch'):
                return {'service': 'checkout'}
            if scenario == 'step_timeout':
                import asyncio

                await asyncio.sleep(1.5)
            raise TimeoutError()

    transport = Transport()
    platform.backend._sop_factory = lambda execution: PolicyToolExecutor(
        platform.backend, transport, execution=execution, delays=(0, 0)
    )
    registry = foundation_worker_registry(platform.backend)
    worker = Worker(platform.backend, 'sop-worker', registry)
    if scenario == 'revoked_at_dispatch':
        original = PolicyToolExecutor.execute

        async def revoke_before_dispatch(self, *args, **kwargs):
            if args[3] == other_ref:
                rules['experts']['diagnosis'] = ['other.read']
            return await original(self, *args, **kwargs)

        monkeypatch.setattr(PolicyToolExecutor, 'execute', revoke_before_dispatch)
    if scenario in ('scope_change', 'revoked'):
        original = PolicyToolExecutor.execute

        async def change_after(self, *args, **kwargs):
            result = await original(self, *args, **kwargs)
            if scenario == 'revoked':
                rules['experts']['diagnosis'] = []
            else:
                async with platform.backend.create_uow().begin_write() as uow:
                    await platform.contexts.query_scope(
                        uow, platform.auth, run.run_id, {'service': 'other'}
                    )
            return result

        monkeypatch.setattr(PolicyToolExecutor, 'execute', change_after)
    if scenario in ('crash', 'success_crash'):
        original = PolicyToolExecutor.execute

        async def crash_after(self, *args, **kwargs):
            await original(self, *args, **kwargs)
            raise RuntimeError('crash after tool receipt')

        monkeypatch.setattr(PolicyToolExecutor, 'execute', crash_after)
        assert await worker.run_once() == 'recovery_scheduled'
        monkeypatch.setattr(PolicyToolExecutor, 'execute', original)
        assert (await registry.reconciler.recover(run.run_id)).action == 'continue'
    assert await worker.run_once() == 'projected'
    view = await platform.get_run(run.run_id)
    assert view.status == 'partial'
    if scenario == 'success_crash':
        assert not any('other:' in text for text in view.outcome['unresolved_items'])
    elif scenario in ('scope_change', 'revoked', 'revoked_at_dispatch', 'optional_failure'):
        assert any('logs: blocked' in text for text in view.outcome['unresolved_items'])
    else:
        assert any(
            'metrics' in text and ('unknown' if scenario == 'crash' else 'timeout') in text
            for text in view.outcome['unresolved_items']
        )
    assert transport.calls == (
        1
        if scenario
        in ('step_timeout', 'success_crash', 'scope_change', 'revoked', 'revoked_at_dispatch')
        else 3
    )


@pytest.mark.asyncio
async def test_sop_rejects_non_workflow_modes_at_admission(platform, client):
    from workbench_agent.domain.profiles import ProfileDraft

    ctx = await platform.contexts.resolve(platform.auth, platform.agent_id)
    ws = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    draft = await platform.profiles.save_draft(
        platform.auth,
        platform.agent_id,
        ProfileDraft(default_model=ctx.default_model, graph_id='sop.conditional'),
        ws['revision'],
        key='v11-draft',
    )
    await platform.profiles.publish(
        platform.auth, platform.agent_id, draft['revision'], key='v11-publish'
    )
    result = await client.post(
        '/v1/runs',
        json={'agent_id': platform.agent_id, 'mode': 'qa', 'input': {'text': 'question'}},
        headers={'Idempotency-Key': 'v11-qa'},
    )
    assert result.status_code == 409
    assert (await platform.count_rows('runs')) == 0


def test_workflow_conditions_and_schemas_are_bounded_and_non_executable():
    valid = {
        'step_id': 'logs',
        'tool': 'logs.read',
        'depends_on': ['metrics'],
        'condition': {'source': 'metrics.service', 'equals': 'checkout'},
        'output_schema': {
            'type': 'object',
            'properties': {'count': {'type': 'integer'}},
            'required': ['count'],
            'additionalProperties': False,
        },
    }
    wf = definition(steps=[{'step_id': 'metrics', 'tool': 'metrics.read'}, valid])
    assert wf.steps[1].condition.source == 'metrics.service'
    with pytest.raises(ValidationError):
        definition(
            steps=[
                {'step_id': 'metrics', 'tool': 'metrics.read'},
                dict(valid, condition={'source': 'missing.value', 'equals': 'x'}),
            ]
        )
    with pytest.raises(ValidationError):
        definition(
            steps=[
                {
                    'step_id': 'metrics',
                    'tool': 'metrics.read',
                    'output_schema': {'$ref': 'https://example.invalid/schema'},
                }
            ]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'scenario',
    [
        'branch_taken',
        'branch_skipped',
        'missing_field',
        'type_mismatch',
        'invalid_output',
        'branch_crash',
    ],
)
async def test_v12_worker_enforces_conditions_and_output_schema(platform, scenario, monkeypatch):
    from workbench_agent.domain.profiles import ProfileDraft
    from workbench_agent.domain.runs import CreateRun
    from workbench_agent.domain.tools import ToolBinding
    from workbench_agent.application.runs import RunService
    from workbench_agent.tools.registry import ToolRegistry
    from workbench_agent.tools.permissions import DeploymentToolPermissions
    from workbench_agent.tools.policy import PolicyToolExecutor
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry
    from workbench_agent.jobs.worker import Worker

    binding = ToolBinding(
        alias='read',
        provider='mcp',
        tool_name='local.read',
        server='local',
        credential_scope='read',
        permission='read',
        input_schema={
            'type': 'object',
            'properties': {'service': {'type': 'string'}},
            'required': ['service'],
            'additionalProperties': False,
        },
    )
    ref = await ToolRegistry(platform.backend).publish(
        platform.auth, platform.agent_id, binding, key='v12-tool'
    )
    ctx = await platform.contexts.resolve(platform.auth, platform.agent_id)
    ws = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    draft = await platform.profiles.save_draft(
        platform.auth,
        platform.agent_id,
        ProfileDraft(default_model=ctx.default_model, graph_id='sop.conditional', tools=(ref,)),
        ws['revision'],
        key='v12-draft',
    )
    await platform.profiles.publish(
        platform.auth, platform.agent_id, draft['revision'], key='v12-profile'
    )
    first = {
        'step_id': 'metrics',
        'tool': 'read',
        'arguments': {'service': 'checkout'},
        'output_schema': {
            'type': 'object',
            'properties': {'service': {'type': 'string'}},
            'required': ['service'],
            'additionalProperties': False,
        },
    }
    second = {
        'step_id': 'logs',
        'tool': 'read',
        'depends_on': ['metrics'],
        'arguments': {'service': 'checkout'},
        'condition': {
            'source': 'metrics.missing' if scenario == 'missing_field' else 'metrics.service',
            'equals': True
            if scenario == 'type_mismatch'
            else ('other' if scenario == 'branch_skipped' else 'checkout'),
        },
    }
    if scenario == 'type_mismatch':
        first['output_schema']['properties']['service']['type'] = 'integer'
    wf = await create_workflow_version(platform, definition(steps=[first, second]))
    request = CreateRun.model_validate(
        {
            'agent_id': platform.agent_id,
            'mode': 'sop',
            'workflow_version_id': wf.workflow_version_id,
            'input': {'text': 'check'},
            'summary_token_reserve': 8000,
        }
    )
    run = await RunService(platform.backend).create(platform.auth, request, 'v12-run')
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.contexts.query_scope(uow, platform.auth, run.run_id, {'service': 'checkout'})
    rules = {
        'users': {platform.auth.subject: ['read']},
        'teams': {platform.auth.team_id: ['read']},
        'agents': {platform.agent_id: ['read']},
        'skills': {'__none__': ['read']},
        'experts': {'diagnosis': ['read']},
    }
    platform.backend._tool_permissions = DeploymentToolPermissions(lambda: rules)

    class Transport:
        calls = 0

        async def call(self, *args, **kwargs):
            self.calls += 1
            return {
                'service': 1
                if scenario == 'type_mismatch'
                else (123 if scenario == 'invalid_output' else 'checkout')
            }

    transport = Transport()
    platform.backend._sop_factory = lambda execution: PolicyToolExecutor(
        platform.backend, transport, execution=execution, delays=(0, 0)
    )
    registry = foundation_worker_registry(platform.backend)
    worker = Worker(platform.backend, 'v12-worker', registry)
    if scenario == 'branch_crash':
        original = PolicyToolExecutor.execute

        async def crash_after(self, *args, **kwargs):
            await original(self, *args, **kwargs)
            raise RuntimeError('crash after receipt')

        monkeypatch.setattr(PolicyToolExecutor, 'execute', crash_after)
        assert await worker.run_once() == 'recovery_scheduled'
        monkeypatch.setattr(PolicyToolExecutor, 'execute', original)
        assert (await registry.reconciler.recover(run.run_id)).action == 'continue'
    assert await worker.run_once() == 'projected'
    async with platform.backend.create_uow().begin_read() as uow:
        saved = await platform.backend.runs.checkpoint(uow, platform.auth.team_id, run.run_id)
    results = saved['values']['results']
    assert results['metrics']['status'] == (
        'invalid_output' if scenario == 'invalid_output' else 'ok'
    )
    assert (
        results['logs']['status']
        == {
            'branch_taken': 'ok',
            'branch_skipped': 'skipped',
            'missing_field': 'invalid_input',
            'type_mismatch': 'skipped',
            'invalid_output': 'blocked',
            'branch_crash': 'ok',
        }[scenario]
    )
    assert transport.calls == (2 if scenario in ('branch_taken', 'branch_crash') else 1)
    assert (await platform.get_run(run.run_id)).status == 'partial'


@pytest.mark.asyncio
async def test_bootstrap_workflow_seed_is_create_if_missing(platform):
    directory = platform.settings.asset_root / 'config' / 'workflows'
    directory.mkdir(parents=True)
    path = directory / 'payment-check.yaml'
    path.write_text('name: payment-check\nsteps:\n  - step_id: metrics\n    tool: metrics.read\n')
    await platform.bootstrap()
    async with platform.backend.create_uow().begin_read() as uow:
        rows = await platform.backend.configuration.rows(
            uow,
            'workflow_versions',
            team_id=platform.auth.team_id,
            agent_id=platform.agent_id,
            name='payment-check',
        )
    assert len(rows) == 1 and rows[0]['version'] == 1
    original = rows[0]['hash']
    path.write_text('name: payment-check\nsteps:\n  - step_id: changed\n    tool: logs.read\n')
    await platform.bootstrap()
    async with platform.backend.create_uow().begin_read() as uow:
        rows = await platform.backend.configuration.rows(
            uow,
            'workflow_versions',
            team_id=platform.auth.team_id,
            agent_id=platform.agent_id,
            name='payment-check',
        )
    assert len(rows) == 1 and rows[0]['hash'] == original


@pytest.mark.asyncio
async def test_conditional_sop_accepts_advanced_workflows(platform):
    from workbench_agent.domain.profiles import ProfileDraft
    from workbench_agent.domain.runs import CreateRun
    from workbench_agent.application.runs import RunService

    ctx = await platform.contexts.resolve(platform.auth, platform.agent_id)
    ws = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    draft = await platform.profiles.save_draft(
        platform.auth,
        platform.agent_id,
        ProfileDraft(default_model=ctx.default_model, graph_id='sop.conditional'),
        ws['revision'],
        key='old-draft',
    )
    await platform.profiles.publish(
        platform.auth, platform.agent_id, draft['revision'], key='old-publish'
    )
    wf = await create_workflow_version(
        platform,
        definition(steps=[{'step_id': 'a', 'tool': 'read', 'output_schema': {'type': 'object'}}]),
    )
    request = CreateRun.model_validate(
        {
            'agent_id': platform.agent_id,
            'mode': 'sop',
            'workflow_version_id': wf.workflow_version_id,
            'input': {'text': 'check'},
        }
    )
    accepted = await RunService(platform.backend).create(platform.auth, request, 'old-run')
    assert accepted.run_id and await platform.count_rows('runs') == 1


@pytest.mark.asyncio
async def test_bootstrap_rejects_symlink_seed_without_partial_publication(platform):
    directory = platform.settings.asset_root / 'config' / 'workflows'
    directory.mkdir(parents=True)
    (directory / 'payment-check.yaml').symlink_to('/etc/passwd')
    with pytest.raises((ValueError, OSError)):
        await platform.bootstrap()
    assert await platform.count_rows('workflow_versions') == 0


@pytest.mark.asyncio
async def test_workflow_fixture_revalidates_mutated_schema(platform):
    wf = definition(steps=[{'step_id': 'a', 'tool': 'read', 'output_schema': {'type': 'object'}}])
    wf.steps[0].output_schema['$ref'] = 'https://example.invalid/schema'
    with pytest.raises(ValidationError):
        await create_workflow_version(platform, wf)
    assert await platform.count_rows('workflow_versions') == 0
