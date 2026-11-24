"""Opt-in real Podman socket test; creates and removes only random test resources."""

import asyncio
import hashlib
import os
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest
from workbench_agent.application.repository_identity import snapshot_identity_hash
from workbench_agent.domain.profiles import ProfileDraft, VersionRef
from workbench_agent.jobs.worker import ExecutorRegistry, Worker
from workbench_agent.runner.service import ContainerRunner, validate_docker_host
from workbench_agent.skill_management.objects import LocalSkillObjects
from support.skills import execution_for_run
from workbench_agent.tools.permissions import DeploymentToolPermissions
from workbench_agent.tools.execution_executor import ExecutionBinding, ExecutionExecutor
from workbench_agent.execution.repositories import RepositoryService, RepositorySyncWorker

pytestmark = pytest.mark.live


async def test_podman_pinned_snapshot_worker_and_timeout(platform, tmp_path, monkeypatch):
    binary = os.environ.get('WORKBENCH_TEST_DOCKER_BINARY')
    host = os.environ.get('WORKBENCH_TEST_DOCKER_HOST')
    image = os.environ.get('WORKBENCH_TEST_IMAGE')
    if not all((binary, host, image)):
        pytest.skip('explicit local Podman test settings required')
    if not Path(binary).is_absolute():
        pytest.fail('absolute Docker CLI required')
    try:
        validate_docker_host(host)
    except ValueError:
        pytest.fail('local Unix socket required')
    if image != 'sha256:c1da6820c78d7d73332b364794386d1a7341f23ab90f8304745a4d9f9c8b8218':
        pytest.fail('explicit approved test image required')
    env = {'PATH': '/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin', 'DOCKER_HOST': host}
    token = uuid4().hex
    marker = 'dod-runner-identity-' + token
    snapshot = 'dod-snapshot-' + token

    def docker(*args):
        return subprocess.run([binary, *args], env=env, check=True, capture_output=True, timeout=30)

    marker_created = snapshot_created = False
    try:
        docker('volume', 'create', marker)
        marker_created = True
        docker('volume', 'create', snapshot)
        snapshot_created = True
        content = b'print("pinned-live-ok")\n'
        docker(
            'run',
            '--rm',
            '--pull=never',
            '--network=none',
            '--mount=type=volume,src=' + snapshot + ',dst=/workspace',
            image,
            '/usr/bin/python3',
            '-c',
            'from pathlib import Path; Path("/workspace/check.py").write_bytes('
            + repr(content)
            + ')',
        )
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
        (repo / 'check.py').write_bytes(content)
        git('add', 'check.py')
        git('commit', '-qm', 'first')
        commit = git('rev-parse', 'HEAD')
        objects = LocalSkillObjects(tmp_path / 'objects')
        await RepositoryService(platform.backend).sync(
            platform.auth, platform.agent_id, 'app', commit, key='sync'
        )
        registry = ExecutorRegistry()
        registry.register_admin(
            'repo_sync',
            RepositorySyncWorker(
                platform.backend, objects, {(platform.auth.team_id, platform.agent_id, 'app'): repo}
            ).execute,
        )
        assert (
            await Worker(platform.backend, 'repo', registry).run_admin_once() == 'admin_completed'
        )
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
        updated = await platform.profiles.save_draft(
            platform.auth,
            platform.agent_id,
            ProfileDraft(
                default_model=current.default_model,
                graph_id='conversation.multi_turn',
                repositories=(ref,),
            ),
            agent['revision'],
            key=str(uuid4()),
        )
        await platform.profiles.publish(
            platform.auth, platform.agent_id, updated['revision'], key=str(uuid4())
        )
        rules = {
            'users': {platform.auth.subject: ['runner.execute']},
            'teams': {platform.auth.team_id: ['runner.execute']},
            'agents': {platform.agent_id: ['runner.execute']},
            'experts': {'diagnosis': ['runner.execute']},
            'skills': {'__none__': ['runner.execute']},
        }
        platform.backend._tool_permissions = DeploymentToolPermissions(lambda: rules)
        monkeypatch.setenv('DEEPSEEK_API_KEY', 'synthetic-fixture-only')
        run = await platform.create_run()
        execution = await execution_for_run(platform, run)
        binding = ExecutionBinding(
            team_id=platform.auth.team_id,
            agent_id=platform.agent_id,
            snapshot_ref=ref,
            image=image,
            snapshot_volume=snapshot,
            script='check.py',
            script_sha256=hashlib.sha256(content).hexdigest(),
            interpreter='python3',
        )
        platform.backend._runner_bindings = {'check': binding}
        platform.backend._runner = ContainerRunner(
            binary, docker_host=host, identity_mode='podman_volume'
        )
        platform.backend._snapshot_objects = objects
        platform.backend._runner_daemon_id = 'podman:' + token
        result = await ExecutionExecutor(platform.backend, execution).execute(
            'check', invocation_id='live'
        )
        assert (
            result.status == 'completed'
            and result.exit_code == 0
            and b'pinned-live-ok' in result.stdout
        )
        receipt = await ExecutionExecutor(platform.backend, execution).receipt('live')
        assert receipt['status'] == 'succeeded' and 'pinned-live-ok' in receipt['stdout']
        # A changed file in the mounted volume must never be executed as the pinned commit.
        docker(
            'run',
            '--rm',
            '--pull=never',
            '--network=none',
            '--mount=type=volume,src=' + snapshot + ',dst=/workspace',
            image,
            '/usr/bin/python3',
            '-c',
            'from pathlib import Path; Path("/workspace/check.py").write_text("print(\\"wrong\\")")',
        )
        altered = await ExecutionExecutor(platform.backend, execution).execute(
            'check', invocation_id='altered'
        )
        assert altered.exit_code != 0 and b'wrong' not in altered.stdout
    finally:
        if snapshot_created:
            docker('volume', 'rm', '-f', snapshot)
        if marker_created:
            docker('volume', 'rm', '-f', marker)


async def test_podman_runner_timeout_and_cancel_remove_parent_child():
    from workbench_agent.runner.policy import RunnerRequest

    binary = os.environ.get('WORKBENCH_TEST_DOCKER_BINARY')
    host = os.environ.get('WORKBENCH_TEST_DOCKER_HOST')
    image = os.environ.get('WORKBENCH_TEST_IMAGE')
    if not all((binary, host, image)):
        pytest.skip('explicit local Podman test settings required')
    if not Path(binary).is_absolute():
        pytest.fail('absolute Docker CLI required')
    try:
        validate_docker_host(host)
    except ValueError:
        pytest.fail('local Unix socket required')
    if image != 'sha256:c1da6820c78d7d73332b364794386d1a7341f23ab90f8304745a4d9f9c8b8218':
        pytest.fail('approved test image required')
    env = {'PATH': '/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin', 'DOCKER_HOST': host}
    token = uuid4().hex
    marker = 'dod-runner-identity-' + token
    snapshot = 'dod-snapshot-' + token

    def docker(*args, check=True):
        return subprocess.run(
            [binary, *args], env=env, check=check, capture_output=True, timeout=30
        )

    made_marker = made_snapshot = False
    try:
        docker('volume', 'create', marker)
        made_marker = True
        docker('volume', 'create', snapshot)
        made_snapshot = True
        runner = ContainerRunner(binary, docker_host=host, identity_mode='podman_volume')
        for mode in ('timeout', 'cancel'):
            nonce = uuid4().hex
            name = 'dod-run-' + nonce
            volume = 'dod-task-' + nonce
            request = RunnerRequest(
                image=image,
                argv=[
                    '/usr/bin/python3',
                    '-c',
                    'import os,time; os.fork(); print("started",flush=True); time.sleep(300)',
                ],
                snapshot_volume=snapshot,
                task_volume=volume,
                container_name=name,
                daemon_id='podman:' + token,
                timeout_seconds=5 if mode == 'timeout' else 60,
            )
            task = asyncio.create_task(runner.execute(request))
            if mode == 'timeout':
                result = await task
                assert result.status == 'timeout' and result.stdout.count(b'started') == 2
            else:
                for _ in range(100):
                    probe = await asyncio.to_thread(
                        docker, 'inspect', '--format', '{{.State.Running}}', name, check=False
                    )
                    if probe.returncode == 0 and probe.stdout.strip() == b'true':
                        break
                    await asyncio.sleep(0.1)
                else:
                    pytest.fail('test container did not start')
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert docker('inspect', name, check=False).returncode != 0
            assert docker('volume', 'inspect', volume, check=False).returncode != 0
    finally:
        if made_snapshot:
            docker('volume', 'rm', '-f', snapshot)
        if made_marker:
            docker('volume', 'rm', '-f', marker)
