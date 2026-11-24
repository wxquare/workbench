from uuid import uuid4
import pytest
from support.skills import execution_for_run
from support.jobs import expire_lease
from workbench_agent.runner.cleanup import RunnerCleanup
from workbench_agent.runner.service import ContainerRunner


async def test_cleanup_obligation_survives_lost_guard_and_recovery_retries(platform, tmp_path):
    run = await platform.create_run()
    execution = await execution_for_run(platform, run)
    guard = execution.guard
    log = tmp_path / 'calls'
    flag = tmp_path / 'fail'
    flag.write_text('1')
    cli = tmp_path / 'docker'
    cli.write_text(
        '#!/usr/bin/env python3\nimport sys\nfrom pathlib import Path\nPath('
        + repr(str(log))
        + ').open("a").write(" ".join(sys.argv[1:3])+"\\n")\nif sys.argv[1]=="info": print("test-daemon");sys.exit(0)\nif Path('
        + repr(str(flag))
        + ').exists(): sys.exit(1)\n'
    )
    cli.chmod(0o755)
    runner = ContainerRunner(str(cli))
    cleanup = RunnerCleanup(platform.backend, runner, 'test-daemon')
    invocation = str(uuid4())
    name = 'dod-run-' + uuid4().hex
    volume = 'dod-task-' + uuid4().hex
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.backend.lease_guard.require(uow, guard, {'running'})
        now = await platform.backend.clock.now_ms(uow)
        await platform.backend.tools.insert_invocation(
            uow,
            dict(
                team_id=guard.team_id,
                invocation_id=invocation,
                run_id=guard.run_id,
                logical_call_id='cleanup',
                tool_version='snapshot',
                args_hash='0' * 64,
                attempt=1,
                status='possibly_sent',
                receipt_ref=None,
                created_at=now,
                updated_at=now,
            ),
        )
        await cleanup.register(uow, guard, invocation, name, volume)
        for step in ('volume_dispatch', 'volume_ack', 'container_dispatch', 'container_ack'):
            await cleanup.step(uow, guard, invocation, step)
    assert await cleanup.recover_once() == 'idle'
    assert not log.exists()
    await expire_lease(platform, guard)
    from workbench_agent.persistence.errors import StaleExecution

    with pytest.raises(StaleExecution):
        async with platform.backend.create_uow().begin_write() as uow:
            await cleanup.mark_clean(uow, guard, invocation)
    assert await RunnerCleanup(platform.backend, runner, 'other-daemon').recover_once() == 'idle'
    assert not log.exists()
    assert await cleanup.recover_once() == 'retry'
    async with platform.backend.create_uow().begin_read() as uow:
        row = await platform.backend.runner_cleanup.get(uow, guard.team_id, invocation)
        assert row['status'] == 'pending' and row['attempts'] == 1
    flag.unlink()
    assert await cleanup.recover_once() == 'idle'
    from sqlalchemy import update
    from workbench_agent.persistence.schema import runner_cleanup_obligations

    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(runner_cleanup_obligations)
            .where(runner_cleanup_obligations.c.invocation_id == invocation)
            .values(next_retry_at=0)
        )
    assert await cleanup.recover_once() == 'cleaned'
    async with platform.backend.create_uow().begin_read() as uow:
        row = await platform.backend.runner_cleanup.get(uow, guard.team_id, invocation)
        assert row['status'] == 'cleaned' and row['attempts'] == 2
    assert log.read_text().count('rm -f') >= 2 and log.read_text().count('volume rm') >= 2


async def test_real_worker_lease_loss_keeps_failed_cleanup_recoverable(platform, tmp_path):
    import asyncio, hashlib, subprocess
    from sqlalchemy import select, update
    from workbench_agent.persistence import schema as s
    from workbench_agent.domain.profiles import ProfileDraft, VersionRef
    from workbench_agent.application.repository_identity import snapshot_identity_hash
    from workbench_agent.jobs.worker import ExecutorRegistry, Worker
    from support.jobs import ReleasingProjector
    from workbench_agent.skill_management.objects import LocalSkillObjects
    from workbench_agent.tools.permissions import DeploymentToolPermissions
    from workbench_agent.tools.execution_executor import ExecutionBinding, ExecutionExecutor
    from workbench_agent.execution.repositories import RepositoryService, RepositorySyncWorker

    repo = tmp_path / 'repo'
    repo.mkdir()

    def git(*args):
        return (
            subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.DEVNULL)
            .decode()
            .strip()
        )

    git('init', '-q')
    git('config', 'user.name', 'Tester')
    git('config', 'user.email', 'test@example.invalid')
    content = b'print("safe")\n'
    (repo / 'check.py').write_bytes(content)
    git('add', 'check.py')
    git('commit', '-qm', 'initial')
    commit = git('rev-parse', 'HEAD')
    objects = LocalSkillObjects(tmp_path / 'objects')
    await RepositoryService(platform.backend).sync(
        platform.auth, platform.agent_id, 'app', commit, key='cleanup-sync'
    )
    admin = ExecutorRegistry()
    admin.register_admin(
        'repo_sync',
        RepositorySyncWorker(
            platform.backend, objects, {(platform.auth.team_id, platform.agent_id, 'app'): repo}
        ).execute,
    )
    assert await Worker(platform.backend, 'admin', admin).run_admin_once() == 'admin_completed'
    async with platform.backend.create_uow().begin_read() as uow:
        row = dict(
            await platform.backend.configuration.one(
                uow,
                'repo_snapshots',
                team_id=platform.auth.team_id,
                agent_id=platform.agent_id,
                repo_id='app',
                commit_sha=commit,
            )
        )
    ref = VersionRef(
        resource_id=row['snapshot_id'], version=commit, content_hash=snapshot_identity_hash(row)
    )
    current = await platform.contexts.resolve(platform.auth, platform.agent_id)
    agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    draft = ProfileDraft(
        default_model=current.default_model, graph_id='conversation.multi_turn', repositories=(ref,)
    )
    changed = await platform.profiles.save_draft(
        platform.auth, platform.agent_id, draft, agent['revision'], key=str(uuid4())
    )
    await platform.profiles.publish(
        platform.auth, platform.agent_id, changed['revision'], key=str(uuid4())
    )
    rules = {
        'users': {platform.auth.subject: ['runner.execute']},
        'teams': {platform.auth.team_id: ['runner.execute']},
        'agents': {platform.agent_id: ['runner.execute']},
        'experts': {'diagnosis': ['runner.execute']},
        'skills': {'__none__': ['runner.execute']},
    }
    platform.backend._tool_permissions = DeploymentToolPermissions(lambda: rules)
    log = tmp_path / 'calls'
    flag = tmp_path / 'fail'
    flag.write_text('1')
    cli = tmp_path / 'docker'
    cli.write_text(
        '#!/usr/bin/env python3\nimport sys,time\nfrom pathlib import Path\nPath('
        + repr(str(log))
        + ').open("a").write(" ".join(sys.argv[1:3])+"\\n")\nif sys.argv[1] == "info": print("daemon-one");sys.exit(0)\nif sys.argv[1] == "start": time.sleep(60)\nif sys.argv[1] == "rm" and Path('
        + repr(str(flag))
        + ').exists(): sys.exit(1)\n'
    )
    cli.chmod(0o755)
    runner = ContainerRunner(str(cli), cleanup_timeout=1)
    binding = ExecutionBinding(
        platform.auth.team_id,
        platform.agent_id,
        ref,
        'sha256:' + 'a' * 64,
        'dod-snapshot-' + 'b' * 32,
        'check.py',
        hashlib.sha256(content).hexdigest(),
        'python',
    )
    platform.backend._runner_bindings = {'check': binding}
    platform.backend._runner = runner
    platform.backend._snapshot_objects = objects
    platform.backend._runner_daemon_id = 'daemon-one'

    class Graph:
        async def execute(self, execution):
            return await ExecutionExecutor(platform.backend, execution).execute(
                'check', invocation_id='live-cleanup'
            )

    registry = ExecutorRegistry()
    registry.register('conversation.multi_turn', Graph(), ReleasingProjector(platform.backend))
    run = await platform.create_run()
    worker = Worker(platform.backend, 'run-worker', registry, renew_interval=0.05)
    task = asyncio.create_task(worker.run_once())
    for _ in range(500):
        if log.exists() and 'start -a' in log.read_text():
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError('runner did not start')
    async with platform.backend.create_uow().begin_write() as uow:
        row = (
            (
                await uow.execute(
                    select(s.runner_cleanup_obligations).where(
                        s.runner_cleanup_obligations.c.run_id == run.run_id
                    )
                )
            )
            .mappings()
            .one()
        )
        assert (
            row['status'] == 'pending'
            and row['daemon_id'] == 'daemon-one'
            and row['creation_steps'] == 15
        )
        await uow.execute(
            update(s.execution_leases)
            .where(s.execution_leases.c.owner_run_id == run.run_id)
            .values(expires_at=0)
        )
        await uow.execute(
            update(s.run_jobs).where(s.run_jobs.c.run_id == run.run_id).values(lease_until=0)
        )
    assert await asyncio.wait_for(task, 10) == 'lease_lost'
    async with platform.backend.create_uow().begin_read() as uow:
        saved = await platform.backend.runner_cleanup.get(
            uow, platform.auth.team_id, row['invocation_id']
        )
        assert saved['status'] == 'pending'
    flag.unlink()
    assert await RunnerCleanup(platform.backend, runner, 'daemon-one').recover_once() == 'cleaned'
    async with platform.backend.create_uow().begin_read() as uow:
        saved = await platform.backend.runner_cleanup.get(
            uow, platform.auth.team_id, row['invocation_id']
        )
        assert saved['status'] == 'cleaned'


async def test_cleanup_obligation_requires_owned_invocation(platform):
    from workbench_agent.persistence.errors import StaleExecution

    run = await platform.create_run()
    execution = await execution_for_run(platform, run)
    cleanup = RunnerCleanup(platform.backend, ContainerRunner('/usr/bin/false'), 'test-daemon')
    async with platform.backend.create_uow().begin_write() as uow:
        with pytest.raises((ValueError, StaleExecution)):
            await cleanup.register(
                uow,
                execution.guard,
                str(uuid4()),
                'dod-run-' + uuid4().hex,
                'dod-task-' + uuid4().hex,
            )


async def test_absent_volume_after_unconfirmed_create_remains_pending(platform, tmp_path):
    run = await platform.create_run()
    execution = await execution_for_run(platform, run)
    guard = execution.guard
    cli = tmp_path / 'docker'
    cli.write_text(
        '#!/usr/bin/env python3\nimport sys\nif sys.argv[1]=="info": print("daemon-one");sys.exit(0)\nkind="container" if sys.argv[1]=="rm" else "volume"\nprint("Error: No such "+kind+": "+sys.argv[-1],file=sys.stderr)\nsys.exit(1)\n'
    )
    cli.chmod(0o755)
    cleanup = RunnerCleanup(platform.backend, ContainerRunner(str(cli)), 'daemon-one')
    invocation = str(uuid4())
    name = 'dod-run-' + uuid4().hex
    volume = 'dod-task-' + uuid4().hex
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.backend.lease_guard.require(uow, guard, {'running'})
        now = await platform.backend.clock.now_ms(uow)
        await platform.backend.tools.insert_invocation(
            uow,
            dict(
                team_id=guard.team_id,
                invocation_id=invocation,
                run_id=guard.run_id,
                logical_call_id='late-create',
                tool_version='snapshot',
                args_hash='0' * 64,
                attempt=1,
                status='possibly_sent',
                receipt_ref=None,
                created_at=now,
                updated_at=now,
            ),
        )
        await cleanup.register(uow, guard, invocation, name, volume)
        await cleanup.step(uow, guard, invocation, 'volume_dispatch')
    await expire_lease(platform, guard)
    assert await cleanup.recover_once() == 'retry'
    async with platform.backend.create_uow().begin_read() as uow:
        row = await platform.backend.runner_cleanup.get(uow, guard.team_id, invocation)
        assert row['status'] == 'pending'


async def test_actual_daemon_id_prevents_wrong_host_cleanup_and_query_failure(platform, tmp_path):
    from sqlalchemy import update
    from workbench_agent.persistence.schema import runner_cleanup_obligations

    run = await platform.create_run()
    execution = await execution_for_run(platform, run)
    guard = execution.guard
    log = tmp_path / 'calls'
    mode = tmp_path / 'mode'
    mode.write_text('wrong')
    cli = tmp_path / 'docker'
    cli.write_text(
        '#!/usr/bin/env python3\nimport sys\nfrom pathlib import Path\nmode=Path('
        + repr(str(mode))
        + ').read_text()\nPath('
        + repr(str(log))
        + ').open("a").write(" ".join(sys.argv[1:3])+"\\n")\nif sys.argv[1]=="info":\n  if mode=="error": sys.exit(1)\n  print("engine-B" if mode=="wrong" else "engine-A");sys.exit(0)\nif mode=="switch" and sys.argv[1]=="rm": Path('
        + repr(str(mode))
        + ').write_text("wrong")\nprint("Error: No such "+("container" if sys.argv[1]=="rm" else "volume")+": "+sys.argv[-1],file=sys.stderr)\nsys.exit(1)\n'
    )
    cli.chmod(0o755)
    cleanup = RunnerCleanup(platform.backend, ContainerRunner(str(cli)), 'engine-A')
    invocation = str(uuid4())
    name = 'dod-run-' + uuid4().hex
    volume = 'dod-task-' + uuid4().hex
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.backend.lease_guard.require(uow, guard, {'running'})
        now = await platform.backend.clock.now_ms(uow)
        await platform.backend.tools.insert_invocation(
            uow,
            dict(
                team_id=guard.team_id,
                invocation_id=invocation,
                run_id=guard.run_id,
                logical_call_id='different-engine',
                tool_version='snapshot',
                args_hash='0' * 64,
                attempt=1,
                status='possibly_sent',
                receipt_ref=None,
                created_at=now,
                updated_at=now,
            ),
        )
        await cleanup.register(uow, guard, invocation, name, volume)
        for step in ('volume_dispatch', 'volume_ack', 'container_dispatch', 'container_ack'):
            await cleanup.step(uow, guard, invocation, step)
    await expire_lease(platform, guard)
    assert await cleanup.recover_once() == 'retry'
    assert 'rm -f' not in log.read_text()
    async with platform.backend.create_uow().begin_write() as uow:
        row = await platform.backend.runner_cleanup.get(uow, guard.team_id, invocation)
        assert row['status'] == 'pending'
        await uow.execute(
            update(runner_cleanup_obligations)
            .where(runner_cleanup_obligations.c.invocation_id == invocation)
            .values(next_retry_at=0)
        )
    mode.write_text('error')
    assert await cleanup.recover_once() == 'retry'
    assert 'rm -f' not in log.read_text()
    async with platform.backend.create_uow().begin_write() as uow:
        row = await platform.backend.runner_cleanup.get(uow, guard.team_id, invocation)
        assert row['status'] == 'pending'
        await uow.execute(
            update(runner_cleanup_obligations)
            .where(runner_cleanup_obligations.c.invocation_id == invocation)
            .values(next_retry_at=0)
        )
    mode.write_text('switch')
    assert await cleanup.recover_once() == 'retry'
    async with platform.backend.create_uow().begin_write() as uow:
        row = await platform.backend.runner_cleanup.get(uow, guard.team_id, invocation)
        assert row['status'] == 'pending'
        await uow.execute(
            update(runner_cleanup_obligations)
            .where(runner_cleanup_obligations.c.invocation_id == invocation)
            .values(next_retry_at=0)
        )
    mode.write_text('right')
    assert await cleanup.recover_once() == 'cleaned'
    assert 'rm -f' in log.read_text() and 'volume rm' in log.read_text()
