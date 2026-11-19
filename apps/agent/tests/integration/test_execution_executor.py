import hashlib
import subprocess
from uuid import uuid4
import pytest
from workbench_agent.application.repository_identity import snapshot_identity_hash
from workbench_agent.domain.profiles import ProfileDraft, VersionRef
from workbench_agent.jobs.worker import ExecutorRegistry, Worker
from workbench_agent.runner.service import ContainerRunner
from workbench_agent.skill_management.objects import LocalSkillObjects
from support.skills import execution_for_run
from workbench_agent.tools.permissions import DeploymentToolPermissions
from workbench_agent.tools.execution_executor import ExecutionExecutor, ExecutionBinding
from workbench_agent.execution.repositories import RepositoryService, RepositorySyncWorker


async def test_executor_dispatches_only_pinned_approved_script_and_charges_budget(platform,tmp_path):
    repo=tmp_path/'repo';repo.mkdir()
    def git(*args): return subprocess.check_output(['git','-C',str(repo),*args],stderr=subprocess.DEVNULL).decode().strip()
    git('init','-q');git('config','user.name','Tester');git('config','user.email','test@example.invalid')
    content=b'print("ok")\n'; (repo/'check.py').write_bytes(content)
    git('add','check.py');git('commit','-qm','first');commit=git('rev-parse','HEAD')
    objects=LocalSkillObjects(tmp_path/'objects')
    await RepositoryService(platform.backend).sync(platform.auth,platform.agent_id,'app',commit,key='sync')
    registry=ExecutorRegistry();registry.register_admin('repo_sync',RepositorySyncWorker(platform.backend,objects,{(platform.auth.team_id,platform.agent_id,'app'):repo}).execute)
    assert await Worker(platform.backend,'repo',registry).run_admin_once()=='admin_completed'
    async with platform.backend.create_uow().begin_read() as uow:
        row=dict(await platform.backend.configuration.one(uow,'repo_snapshots',team_id=platform.auth.team_id,agent_id=platform.agent_id,repo_id='app',commit_sha=commit))
    ref=VersionRef(resource_id=row['snapshot_id'],version=commit,content_hash=snapshot_identity_hash(row))
    current=await platform.contexts.resolve(platform.auth,platform.agent_id); agent=await platform.profiles.get_agent(platform.auth,platform.agent_id)
    draft=ProfileDraft(default_model=current.default_model,graph_id='conversation.multi_turn',repositories=(ref,))
    updated=await platform.profiles.save_draft(platform.auth,platform.agent_id,draft,agent['revision'],key=str(uuid4()))
    await platform.profiles.publish(platform.auth,platform.agent_id,updated['revision'],key=str(uuid4()))
    rules={'users':{platform.auth.subject:['runner.execute']},'teams':{platform.auth.team_id:['runner.execute']},'agents':{platform.agent_id:['runner.execute']},'experts':{'diagnosis':['runner.execute']},'skills':{'__none__':['runner.execute']}}
    platform.backend._tool_permissions=DeploymentToolPermissions(lambda:rules)
    run=await platform.create_run(); execution=await execution_for_run(platform,run)
    cli=tmp_path/'docker';cli.write_text('#!/bin/sh\nif [ \"$1\" = info ]; then echo test-daemon; fi\nexit 0\n');cli.chmod(0o755)
    binding=ExecutionBinding(team_id=platform.auth.team_id,agent_id=platform.agent_id,snapshot_ref=ref, image='sha256:'+'a'*64, snapshot_volume='dod-snapshot-'+'b'*32,script='check.py',script_sha256=hashlib.sha256(content).hexdigest(),interpreter='python')
    platform.backend._runner_bindings={'check':binding};platform.backend._runner=ContainerRunner(str(cli));platform.backend._snapshot_objects=objects;platform.backend._runner_daemon_id='test-daemon'
    executor=ExecutionExecutor(platform.backend,execution)
    result=await executor.execute('check',invocation_id='first')
    assert result.status=='completed'
    async with platform.backend.create_uow().begin_read() as uow:
        invocation=await platform.backend.tools.invocation(uow,platform.auth.team_id,run.run_id,'first')
        cleanup=await platform.backend.runner_cleanup.get(uow,platform.auth.team_id,invocation['invocation_id'])
        assert cleanup and cleanup['status']=='cleaned' and cleanup['daemon_id']=='test-daemon'
    receipt=await executor.receipt('first')
    assert receipt['status']=='succeeded' and receipt['exit_code']==0
    from workbench_agent.runtime.budgets.service import BudgetService
    assert (await BudgetService(platform.backend).snapshot(platform.auth,run.run_id)).tool_attempts==1
    with pytest.raises(Exception): await executor.execute('check',invocation_id='first')
    from dataclasses import replace
    from workbench_agent.application.errors import DomainError
    platform.backend._runner_bindings['check']=replace(binding,script_sha256='0'*64)
    with pytest.raises(DomainError) as error:
        await executor.execute('check',invocation_id='second')
    assert error.value.code == 'RUNNER_SCRIPT_UNAVAILABLE'
    assert (await BudgetService(platform.backend).snapshot(platform.auth,run.run_id)).tool_attempts==1
    platform.backend._runner_bindings['check']=binding
    rules['experts']['diagnosis']=[]
    with pytest.raises(DomainError) as error:
        await executor.execute('check',invocation_id='third')
    assert error.value.code == 'FORBIDDEN'
    assert (await BudgetService(platform.backend).snapshot(platform.auth,run.run_id)).tool_attempts==1
    rules['experts']['diagnosis']=['runner.execute']
    from workbench_agent.runner.service import RunnerResult
    class RevokingRunner(ContainerRunner):
        async def execute(self, request):
            rules['experts']['diagnosis']=[]
            return RunnerResult('completed',0,b'ok',b'')
    platform.backend._runner=RevokingRunner(str(cli))
    with pytest.raises(DomainError) as error:
        await executor.execute('check',invocation_id='fourth')
    assert error.value.code == 'FORBIDDEN'
    assert (await BudgetService(platform.backend).snapshot(platform.auth,run.run_id)).tool_attempts==2
    rules['experts']['diagnosis']=['runner.execute']
    class FixedRunner(ContainerRunner):
        def __init__(self,result): super().__init__(str(cli)); self.result=result
        async def execute(self, request): return self.result
    for name,value,status in [('timeout',RunnerResult('timeout',None,b'partial',b''),'timeout'),('nonzero',RunnerResult('completed',7,b'result: 42 password=top-secret',b'error'),'failed')]:
        platform.backend._runner=FixedRunner(value)
        await executor.execute('check',invocation_id=name)
        saved=await executor.receipt(name)
        assert saved['status']==status and saved['exit_code']==value.exit_code
        if name=='nonzero':
            assert 'result: 42' in saved['stdout']
            assert 'top-secret' not in str(saved)
    from workbench_agent.persistence.backends.sqlite import SQLiteBackend
    restarted=SQLiteBackend(platform.settings.storage.database_url.get_secret_value())
    try:
        async with restarted.create_uow().begin_read() as uow:
            row=await restarted.tools.invocation(uow,platform.auth.team_id,run.run_id,'nonzero')
            import json
            restored=json.loads(row['receipt_ref'])
            assert row['status']=='failed' and 'result: 42' in restored['stdout']
            assert 'top-secret' not in str(restored)
    finally: await restarted.close()
    class BrokenRunner(ContainerRunner):
        async def execute(self,request): raise RuntimeError('unknown dispatch')
    platform.backend._runner=BrokenRunner(str(cli))
    with pytest.raises(RuntimeError): await executor.execute('check',invocation_id='uncertain')
    assert (await executor.receipt('uncertain'))['status']=='unknown'
    with pytest.raises(DomainError): await executor.execute('check',invocation_id='uncertain')
    from support.jobs import expire_lease
    await expire_lease(platform,execution.guard)
    takeover=await execution_for_run(platform,run)
    recovered=await ExecutionExecutor(platform.backend,takeover).receipt('nonzero')
    assert recovered['status']=='failed' and 'result: 42' in recovered['stdout']
    assert 'top-secret' not in str(recovered)
    from sqlalchemy import update
    from workbench_agent.persistence.schema import runs
    from workbench_agent.persistence.errors import StaleExecution
    class DeadlineRunner(ContainerRunner):
        async def execute(self,request):
            async with platform.backend.create_uow().begin_write() as uow:
                now=await platform.backend.clock.now_ms(uow)
                await uow.execute(update(runs).where(runs.c.run_id==run.run_id).values(wall_deadline_at=now-1))
            return RunnerResult('timeout',None,b'last output',b'')
    platform.backend._runner=DeadlineRunner(str(cli))
    with pytest.raises(StaleExecution): await ExecutionExecutor(platform.backend,takeover).execute('check',invocation_id='deadline')
    async with platform.backend.create_uow().begin_read() as uow:
        row=await platform.backend.tools.invocation(uow,platform.auth.team_id,run.run_id,'deadline')
        import json
        assert row['status']=='timeout' and json.loads(row['receipt_ref'])['stdout']=='last output'


def test_runner_deployment_configuration_defaults_to_no_bindings():
    from workbench_agent.settings import StorageSettings
    assert StorageSettings(database_url='sqlite+aiosqlite:////tmp/example.db').runner_bindings == []
