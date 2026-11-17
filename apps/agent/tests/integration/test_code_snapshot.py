import hashlib
import subprocess
from uuid import uuid4

import pytest

from workbench_agent.application.repository_identity import snapshot_identity_hash
from workbench_agent.domain.profiles import ProfileDraft, VersionRef
from workbench_agent.jobs.worker import ExecutorRegistry, Worker
from workbench_agent.skill_management.objects import LocalSkillObjects
from support.skills import execution_for_run
from workbench_agent.tools.permissions import DeploymentToolPermissions
from workbench_agent.tools.snapshot_reader import SnapshotReader
from workbench_agent.execution.repositories import RepositoryService, RepositorySyncWorker


@pytest.mark.parametrize('source,expected', [
    (b'def calculate():\n    return 42\n', ['def calculate():', '    return 42']),
    ('first\u2028second\u0085third\vfourth\r\nlast\n'.encode(), ['first\u2028second\u0085third\vfourth', 'last']),
])
async def test_guarded_snapshot_read_is_pinned_and_receipt_replays(platform, tmp_path, source, expected):
    repo = tmp_path / 'repo'; repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.DEVNULL).decode().strip()
    git('init', '-q'); git('config', 'user.name', 'Tester'); git('config', 'user.email', 'tester@example.invalid')
    (repo / 'app.py').write_bytes(source); git('add', 'app.py'); git('commit', '-qm', 'first'); commit = git('rev-parse', 'HEAD')
    objects = LocalSkillObjects(tmp_path / 'objects')
    await RepositoryService(platform.backend).sync(platform.auth, platform.agent_id, 'app', commit, key='sync-code')
    reg = ExecutorRegistry(); reg.register_admin('repo_sync', RepositorySyncWorker(platform.backend, objects, {(platform.auth.team_id, platform.agent_id, 'app'): repo}).execute)
    assert await Worker(platform.backend, 'sync-code', reg).run_admin_once() == 'admin_completed'
    async with platform.backend.create_uow().begin_read() as uow:
        row = dict(await platform.backend.configuration.one(uow, 'repo_snapshots', team_id=platform.auth.team_id, agent_id=platform.agent_id, repo_id='app', commit_sha=commit))
    ref = VersionRef(resource_id=row['snapshot_id'], version=commit, content_hash=snapshot_identity_hash(row))
    context = await platform.contexts.resolve(platform.auth, platform.agent_id)
    agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    draft = ProfileDraft(default_model=context.default_model, graph_version='dod.v1', repositories=(ref,))
    changed = await platform.profiles.save_draft(platform.auth, platform.agent_id, draft, agent['revision'], key=str(uuid4()))
    await platform.profiles.publish(platform.auth, platform.agent_id, changed['revision'], key=str(uuid4()))
    permission = 'repository.read'
    rules = {'users': {platform.auth.subject: [permission]}, 'teams': {platform.auth.team_id: [permission]}, 'agents': {platform.agent_id: [permission]}, 'experts': {'diagnosis': [permission]}, 'skills': {'__none__': [permission]}}
    platform.backend._tool_permissions = DeploymentToolPermissions(lambda: rules)
    platform.backend._snapshot_objects = objects
    run = await platform.create_run(key='read-code')
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.contexts.query_scope(uow, platform.auth, run.run_id, {'service': 'app'})
    execution = await execution_for_run(platform, run)
    reader = SnapshotReader(platform.backend, execution)
    (repo / 'app.py').write_text('changed')
    result = await reader.read(ref, 'app.py', 1, 2, invocation_id='logical-read')
    assert result['data']['commit'] == commit
    assert result['data']['lines'] == [{'line': 1, 'text': expected[0]}, {'line': 2, 'text': expected[1]}]
    assert result['data']['sha256'] == hashlib.sha256(source).hexdigest()
    assert result['evidence_ref']
    assert await reader.read(ref, 'app.py', 1, 2, invocation_id='logical-read') == result
    from workbench_agent.runtime.budgets.service import BudgetService
    assert (await BudgetService(platform.backend).snapshot(platform.auth, run.run_id)).tool_attempts == 1
    rules['experts']['diagnosis'] = []
    with pytest.raises(Exception):
        await reader.read(ref, 'app.py', 1, 2, invocation_id='logical-read')


@pytest.mark.parametrize('scenario', ['ok', 'revoked', 'budget', 'tool_crash', 'model_crash', 'scope_change', 'missing'])
async def test_code_graph_reads_pinned_git_with_worker_and_model(platform, tmp_path, monkeypatch, scenario):
    from langchain_core.messages import AIMessage, ToolMessage
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry
    from workbench_agent.runtime.models.invoker import ModelInvoker
    repo = tmp_path / 'graph-repo'; repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.DEVNULL).decode().strip()
    git('init', '-q'); git('config', 'user.name', 'Tester'); git('config', 'user.email', 'tester@example.invalid')
    (repo / 'main.py').write_text('answer = 42\n'); git('add', 'main.py'); git('commit', '-qm', 'first'); commit = git('rev-parse', 'HEAD')
    objects = LocalSkillObjects(tmp_path / 'graph-objects')
    await RepositoryService(platform.backend).sync(platform.auth, platform.agent_id, 'app', commit, key='sync-graph-code')
    reg = ExecutorRegistry(); reg.register_admin('repo_sync', RepositorySyncWorker(platform.backend, objects, {(platform.auth.team_id, platform.agent_id, 'app'): repo}).execute)
    assert await Worker(platform.backend, 'sync-graph-code', reg).run_admin_once() == 'admin_completed'
    async with platform.backend.create_uow().begin_read() as uow:
        row = dict(await platform.backend.configuration.one(uow, 'repo_snapshots', team_id=platform.auth.team_id, agent_id=platform.agent_id, repo_id='app', commit_sha=commit))
    ref = VersionRef(resource_id=row['snapshot_id'], version=commit, content_hash=snapshot_identity_hash(row))
    context = await platform.contexts.resolve(platform.auth, platform.agent_id)
    agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    draft = ProfileDraft(default_model=context.default_model, graph_version='dod.v6', repositories=(ref,))
    changed = await platform.profiles.save_draft(platform.auth, platform.agent_id, draft, agent['revision'], key=str(uuid4()))
    await platform.profiles.publish(platform.auth, platform.agent_id, changed['revision'], key=str(uuid4()))
    permission = 'repository.read'
    rules = {'users': {platform.auth.subject: [permission]}, 'teams': {platform.auth.team_id: [permission]}, 'agents': {platform.agent_id: [permission]}, 'experts': {'code': [permission]}, 'skills': {'__none__': [permission]}}
    if scenario == 'revoked': rules['experts']['code'] = []
    platform.backend._tool_permissions = DeploymentToolPermissions(lambda: rules)
    platform.backend._snapshot_objects = objects
    class Model:
        def bind_tools(self, tools): return self
        async def ainvoke(self, messages, **kwargs):
            usage = {'input_tokens': 8, 'output_tokens': 3, 'total_tokens': 11}
            if any(isinstance(m, ToolMessage) for m in messages):
                return AIMessage(content='The answer is 42.', usage_metadata=usage)
            return AIMessage(content='', tool_calls=[{'name': 'read_snapshot', 'args': {'path': 'main.py', 'start': 1, 'end': 1}, 'id': 'read-1', 'type': 'tool_call'}], usage_metadata=usage)
    class Factory:
        def build(self, *args, **kwargs): return Model()
    platform.backend._code_factory = lambda execution: ModelInvoker(platform.backend, Factory(), object(), execution=execution)
    run = await platform.create_run(key='graph-code', mode='code', summary_token_reserve=8000, model_call_limit=1 if scenario == 'budget' else None)
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.contexts.query_scope(uow, platform.auth, run.run_id, {'service': 'app'})
    if scenario == 'missing':
        (objects.root / row['storage_ref'].split(':', 1)[1]).unlink()
    registry = foundation_worker_registry(platform.backend)
    worker = Worker(platform.backend, 'code-worker', registry)
    if scenario in ('tool_crash', 'model_crash', 'scope_change'):
        from workbench_agent.runtime.experts.code import CodeChatModel
        target, method = (SnapshotReader, 'read') if scenario in ('tool_crash', 'scope_change') else (CodeChatModel, '_agenerate')
        original = getattr(target, method)
        async def crash_after(*args, **kwargs):
            await original(*args, **kwargs)
            raise RuntimeError('crash after operation before checkpoint')
        monkeypatch.setattr(target, method, crash_after)
        assert await worker.run_once() == 'recovery_scheduled'
        monkeypatch.setattr(target, method, original)
        if scenario == 'scope_change':
            async with platform.backend.create_uow().begin_write() as uow:
                await platform.contexts.query_scope(uow, platform.auth, run.run_id, {'service': 'different'})
        assert (await registry.reconciler.recover(run.run_id)).action == 'continue'
    assert await worker.run_once() == 'projected'
    result = await platform.get_run(run.run_id)
    assert result.status == 'partial'
    assert len(result.outcome['evidence_refs']) == (1 if scenario in ('ok', 'tool_crash') else 0)
    if scenario in ('ok', 'tool_crash'):
        async with platform.backend.create_uow().begin_read() as uow:
            evidence = await platform.contexts.evidence_content(uow, platform.auth, run.run_id, str(result.outcome['evidence_refs'][0]))
        assert commit in str(evidence) and 'answer = 42' in str(evidence)
    usage = await platform.budgets.snapshot(platform.auth, run.run_id)
    assert usage.model_attempts == (0 if scenario == 'budget' else 1 if scenario in ('model_crash', 'scope_change', 'revoked', 'missing') else 2)
    assert usage.tool_attempts == (1 if scenario in ('ok', 'tool_crash', 'scope_change', 'missing') else 0)
