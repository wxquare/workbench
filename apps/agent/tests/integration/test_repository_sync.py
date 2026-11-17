import subprocess
import pytest
from workbench_agent.jobs.worker import ExecutorRegistry, Worker
from workbench_agent.skill_management.objects import LocalSkillObjects
from workbench_agent.execution.repositories import RepositoryService, RepositorySyncWorker


async def test_sync_job_pins_approved_commit_and_returns_immutable_snapshot(platform, tmp_path):
    repo = tmp_path / 'repo'; repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.DEVNULL).decode().strip()
    git('init', '-q'); git('config', 'user.name', 'Tester'); git('config', 'user.email', 'test@example.invalid')
    (repo / 'app.py').write_text('first'); git('add', 'app.py'); git('commit', '-qm', 'first')
    commit = git('rev-parse', 'HEAD')
    service = RepositoryService(platform.backend)
    job = await service.sync(platform.auth, platform.agent_id, 'app', commit, key='sync')
    replay = await service.sync(platform.auth, platform.agent_id, 'app', commit, key='sync')
    assert job.job_id == replay.job_id
    objects = LocalSkillObjects(tmp_path / 'objects')
    executor = RepositorySyncWorker(platform.backend, objects, {(platform.auth.team_id, platform.agent_id, 'app'): repo})
    registry = ExecutorRegistry(); registry.register_admin('repo_sync', executor.execute)
    assert await Worker(platform.backend, 'repo-worker', registry).run_admin_once() == 'admin_completed'
    async with platform.backend.create_uow().begin_read() as uow:
        row = await platform.backend.configuration.one(uow, 'repo_snapshots', team_id=platform.auth.team_id, agent_id=platform.agent_id, repo_id='app', commit_sha=commit)
        assert row and row['status'] == 'ready'
        original = objects.get(row['storage_ref'])
    (repo / 'app.py').write_text('changed')
    assert objects.get(row['storage_ref']) == original


async def test_sync_refuses_unapproved_agent_and_revoked_admin(platform, tmp_path):
    from sqlalchemy import update
    from workbench_agent.persistence.schema import team_members
    repo = tmp_path / 'repo'; repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.DEVNULL).decode().strip()
    git('init', '-q'); git('config', 'user.name', 'Tester'); git('config', 'user.email', 'test@example.invalid')
    (repo / 'app.py').write_text('first'); git('add', 'app.py'); git('commit', '-qm', 'first')
    commit = git('rev-parse', 'HEAD')
    await RepositoryService(platform.backend).sync(platform.auth, platform.agent_id, 'app', commit, key='scope')
    objects = LocalSkillObjects(tmp_path / 'objects')
    executor = RepositorySyncWorker(platform.backend, objects, {(platform.auth.team_id, 'different', 'app'): repo})
    registry = ExecutorRegistry(); registry.register_admin('repo_sync', executor.execute)
    assert await Worker(platform.backend, 'wrong-scope', registry).run_admin_once() == 'admin_blocked'
    assert not list((tmp_path / 'objects').iterdir())
    await RepositoryService(platform.backend).sync(platform.auth, platform.agent_id, 'app', commit, key='revoked')
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(update(team_members).where(team_members.c.team_id == platform.auth.team_id, team_members.c.subject == platform.auth.subject).values(status='revoked'))
    executor = RepositorySyncWorker(platform.backend, objects, {(platform.auth.team_id, platform.agent_id, 'app'): repo})
    registry = ExecutorRegistry(); registry.register_admin('repo_sync', executor.execute)
    assert await Worker(platform.backend, 'revoked', registry).run_admin_once() == 'admin_blocked'
    assert not list((tmp_path / 'objects').iterdir())


async def test_http_sync_submits_durable_job_and_replays(client, platform):
    url = f'/v1/agents/{platform.agent_id}/repositories/app/sync'
    body = {'commit': 'a' * 40}
    first = await client.post(url, json=body, headers={'Idempotency-Key': 'sync-http'})
    assert first.status_code == 202
    assert first.json()['kind'] == 'repo_sync'
    again = await client.post(url, json=body, headers={'Idempotency-Key': 'sync-http'})
    assert again.json()['job_id'] == first.json()['job_id']
