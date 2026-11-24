from hashlib import sha256
from workbench_agent.application.artifacts import ArtifactService
from workbench_agent.jobs.worker import ExecutorRegistry, Worker
from workbench_agent.execution.uploads import (
    ArtifactUploadQueue,
    ArtifactUploadWorker,
    UploadStaging,
)
from workbench_agent.skill_management.objects import LocalSkillObjects


class Store:
    def __init__(self, path):
        self.objects = LocalSkillObjects(path)

    async def put_verified(self, data, digest):
        assert sha256(data).hexdigest() == digest
        return self.objects.put(data)


async def test_upload_job_replays_and_worker_verifies_before_available(platform, tmp_path):
    data = b'contents'
    run = await platform.create_run()
    objects = Store(tmp_path / 'objects')
    artifacts = ArtifactService(platform.backend, objects=objects)
    item = await artifacts.begin_upload(
        platform.auth, run.run_id, 'report', sha256(data).hexdigest(), len(data)
    )
    stage = UploadStaging(tmp_path / 'staging')
    queue = ArtifactUploadQueue(platform.backend, stage)
    job = await queue.submit(platform.auth, item['artifact_id'], data, key='upload')
    replay = await queue.submit(platform.auth, item['artifact_id'], data, key='upload')
    assert job.job_id == replay.job_id
    registry = ExecutorRegistry()
    registry.register_admin(
        'artifact_upload', ArtifactUploadWorker(platform.backend, artifacts, stage).execute
    )
    assert (
        await Worker(platform.backend, 'uploader', registry).run_admin_once() == 'admin_completed'
    )
    assert (await artifacts._authorized(platform.auth, item['artifact_id']))[
        'status'
    ] == 'available'


async def test_http_upload_is_persistently_queued(client, platform, tmp_path):
    object.__setattr__(platform.settings.storage, 'upload_staging_root', tmp_path / 'staging')
    run = await platform.create_run()
    data = b'http-upload'
    first = await client.post(
        f'/v1/runs/{run.run_id}/uploads',
        json={'kind': 'report', 'sha256': sha256(data).hexdigest(), 'size': len(data)},
    )
    assert first.status_code == 201
    artifact_id = first.json()['artifact_id']
    upload = await client.post(
        f'/v1/uploads/{artifact_id}/complete',
        content=data,
        headers={'Idempotency-Key': 'http-upload'},
    )
    assert upload.status_code == 202 and upload.json()['kind'] == 'artifact_upload'


async def test_upload_replay_precedes_scope_and_body_and_conflicts_on_new_artifact(
    client, platform, tmp_path
):
    from support.events import evidence

    object.__setattr__(platform.settings.storage, 'upload_staging_root', tmp_path / 'staging')
    run = await platform.create_run()
    data = b'replay'
    service = ArtifactService(platform.backend)
    first = await service.begin_upload(
        platform.auth, run.run_id, 'report', sha256(data).hexdigest(), len(data)
    )
    second = await service.begin_upload(
        platform.auth, run.run_id, 'report', sha256(data).hexdigest(), len(data)
    )
    url = f'/v1/uploads/{first["artifact_id"]}/complete'
    accepted = await client.post(url, content=data, headers={'Idempotency-Key': 'replay-scope'})
    assert accepted.status_code == 202
    await evidence(platform, run, region='PH')
    conflict_body = await client.post(
        url, content=b'invalid', headers={'Idempotency-Key': 'replay-scope'}
    )
    assert conflict_body.status_code == 409
    same_length = await client.post(
        url, content=b'REPLAY', headers={'Idempotency-Key': 'replay-scope'}
    )
    assert same_length.status_code == 409
    replay = await client.post(
        url, headers={'Idempotency-Key': 'replay-scope', 'Upload-Replay': 'true'}
    )
    assert replay.status_code == 202 and replay.json()['job_id'] == accepted.json()['job_id']
    with_body = await client.post(
        url,
        content=b'invalid',
        headers={'Idempotency-Key': 'replay-scope', 'Upload-Replay': 'true'},
    )
    assert with_body.status_code == 422
    conflict = await client.post(
        f'/v1/uploads/{second["artifact_id"]}/complete',
        content=b'ignored',
        headers={'Idempotency-Key': 'replay-scope'},
    )
    assert conflict.status_code == 409


async def test_staging_reaps_orphans_and_completed_but_preserves_queued(platform, tmp_path):
    data = b'staged'
    run = await platform.create_run()
    objects = Store(tmp_path / 'objects')
    artifacts = ArtifactService(platform.backend, objects=objects)
    item = await artifacts.begin_upload(
        platform.auth, run.run_id, 'report', sha256(data).hexdigest(), len(data)
    )
    stage = UploadStaging(tmp_path / 'stage')
    queue = ArtifactUploadQueue(platform.backend, stage)
    _job = await queue.submit(platform.auth, item['artifact_id'], data, key='reap')
    orphan = stage.write(b'orphan')
    await queue.reap()
    assert not (stage.root / orphan).exists()
    assert len([p for p in stage.root.iterdir() if not p.name.startswith('.')]) == 1
    registry = ExecutorRegistry()
    registry.register_admin(
        'artifact_upload', ArtifactUploadWorker(platform.backend, artifacts, stage).execute
    )
    assert (
        await Worker(platform.backend, 'upload-reap', registry).run_admin_once()
        == 'admin_completed'
    )
    await queue.reap()
    assert not [p for p in stage.root.iterdir() if not p.name.startswith('.')]


async def test_staging_quota_counts_pending_and_refuses_overflow(platform, tmp_path):
    from workbench_agent.application.errors import DomainError
    import pytest

    data = b'12345'
    run = await platform.create_run()
    artifacts = ArtifactService(platform.backend)
    one = await artifacts.begin_upload(
        platform.auth, run.run_id, 'report', sha256(data).hexdigest(), len(data)
    )
    two = await artifacts.begin_upload(
        platform.auth, run.run_id, 'report', sha256(data).hexdigest(), len(data)
    )
    stage = UploadStaging(tmp_path / 'stage', max_bytes=5, max_files=2)
    queue = ArtifactUploadQueue(platform.backend, stage)
    await queue.submit(platform.auth, one['artifact_id'], data, key='one')
    with pytest.raises(DomainError) as error:
        await queue.submit(platform.auth, two['artifact_id'], data, key='two')
    assert error.value.code == 'UPLOAD_STAGING_FULL'
    assert len([p for p in stage.root.iterdir() if not p.name.startswith('.')]) == 1


async def test_parallel_duplicate_requests_keep_one_stage(platform, tmp_path):
    import asyncio

    data = b'parallel'
    run = await platform.create_run()
    artifacts = ArtifactService(platform.backend)
    item = await artifacts.begin_upload(
        platform.auth, run.run_id, 'report', sha256(data).hexdigest(), len(data)
    )
    stage = UploadStaging(tmp_path / 'stage', max_bytes=len(data), max_files=1)
    queue = ArtifactUploadQueue(platform.backend, stage)
    one, two = await asyncio.gather(
        *(queue.submit(platform.auth, item['artifact_id'], data, key='same') for _ in range(2))
    )
    assert one.job_id == two.job_id
    assert len([p for p in stage.root.iterdir() if not p.name.startswith('.')]) == 1


async def test_revoked_upload_job_is_terminal_and_reapable(platform, tmp_path):
    from sqlalchemy import update
    from workbench_agent.persistence.schema import team_members

    data = b'revoked'
    run = await platform.create_run()
    artifacts = ArtifactService(platform.backend, objects=Store(tmp_path / 'objects'))
    item = await artifacts.begin_upload(
        platform.auth, run.run_id, 'report', sha256(data).hexdigest(), len(data)
    )
    stage = UploadStaging(tmp_path / 'stage')
    queue = ArtifactUploadQueue(platform.backend, stage)
    await queue.submit(platform.auth, item['artifact_id'], data, key='revoked')
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(team_members)
            .where(
                team_members.c.team_id == platform.auth.team_id,
                team_members.c.subject == platform.auth.subject,
            )
            .values(status='revoked')
        )
    registry = ExecutorRegistry()
    registry.register_admin(
        'artifact_upload', ArtifactUploadWorker(platform.backend, artifacts, stage).execute
    )
    assert (
        await Worker(platform.backend, 'revoked-upload', registry).run_admin_once()
        == 'admin_blocked'
    )
    await queue.reap()
    assert not [p for p in stage.root.iterdir() if not p.name.startswith('.')]


async def test_periodic_reaper_runs_then_stops():
    import asyncio
    from workbench_agent.execution.uploads import reap_periodically

    class Queue:
        def __init__(self):
            self.calls = 0

        async def reap(self):
            self.calls += 1
            stop.set()

    stop = asyncio.Event()
    queue = Queue()
    await reap_periodically(queue, stop, interval=0.01)
    assert queue.calls == 1


async def test_reaper_preserves_file_while_worker_is_in_flight(platform, tmp_path):
    import asyncio

    data = b'in-flight'
    run = await platform.create_run()
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowStore(Store):
        async def put_verified(self, payload, digest):
            started.set()
            await release.wait()
            return await super().put_verified(payload, digest)

    objects = SlowStore(tmp_path / 'objects')
    artifacts = ArtifactService(platform.backend, objects=objects)
    item = await artifacts.begin_upload(
        platform.auth, run.run_id, 'report', sha256(data).hexdigest(), len(data)
    )
    stage = UploadStaging(tmp_path / 'stage')
    queue = ArtifactUploadQueue(platform.backend, stage)
    await queue.submit(platform.auth, item['artifact_id'], data, key='inflight')
    registry = ExecutorRegistry()
    registry.register_admin(
        'artifact_upload', ArtifactUploadWorker(platform.backend, artifacts, stage).execute
    )
    running = asyncio.create_task(Worker(platform.backend, 'inflight', registry).run_admin_once())
    await asyncio.wait_for(started.wait(), 5)
    await queue.reap()
    assert len([p for p in stage.root.iterdir() if not p.name.startswith('.')]) == 1
    release.set()
    assert await running == 'admin_completed'
    await queue.reap()
    assert not [p for p in stage.root.iterdir() if not p.name.startswith('.')]


async def test_staging_owner_rejects_another_database(platform, tmp_path):
    from types import SimpleNamespace
    import pytest

    stage = UploadStaging(tmp_path / 'stage')
    queue = ArtifactUploadQueue(platform.backend, stage)
    await queue.reap()
    foreign = SimpleNamespace(path=tmp_path / 'different.db')
    with pytest.raises(ValueError, match='another database'):
        async with stage.locked(foreign):
            pass


async def test_repeated_storage_failure_becomes_terminal_after_bounded_attempts(platform, tmp_path):
    from sqlalchemy import update, select
    from workbench_agent.persistence.schema import admin_jobs

    data = b'failure'
    run = await platform.create_run()

    class BrokenStore:
        async def put_verified(self, data, digest):
            raise OSError('storage unavailable')

    artifacts = ArtifactService(platform.backend, objects=BrokenStore())
    item = await artifacts.begin_upload(
        platform.auth, run.run_id, 'report', sha256(data).hexdigest(), len(data)
    )
    stage = UploadStaging(tmp_path / 'stage')
    queue = ArtifactUploadQueue(platform.backend, stage)
    job = await queue.submit(platform.auth, item['artifact_id'], data, key='fails')
    registry = ExecutorRegistry()
    registry.register_admin(
        'artifact_upload', ArtifactUploadWorker(platform.backend, artifacts, stage).execute
    )
    worker = Worker(platform.backend, 'retry-upload', registry)
    for attempt in range(3):
        assert await worker.run_admin_once() == 'admin_blocked'
        if attempt < 2:
            async with platform.backend.create_uow().begin_write() as uow:
                await uow.execute(
                    update(admin_jobs)
                    .where(admin_jobs.c.job_id == job.job_id)
                    .values(lease_until=0)
                )
    async with platform.backend.create_uow().begin_read() as uow:
        assert (
            await uow.execute(select(admin_jobs.c.status).where(admin_jobs.c.job_id == job.job_id))
        ).scalar_one() == 'failed'
    await queue.reap()
    assert not [p for p in stage.root.iterdir() if not p.name.startswith('.')]


async def test_staging_owner_marker_survives_interrupted_publication(
    platform, tmp_path, monkeypatch
):
    import os
    import pytest

    stage = UploadStaging(tmp_path / 'stage')
    _link = os.link

    def fail(*args, **kwargs):
        raise OSError('simulated interruption')

    with monkeypatch.context() as patch:
        patch.setattr(os, 'link', fail)
        with pytest.raises(OSError):
            async with stage.locked(platform.backend):
                pass
    assert not (stage.root / '.owner').exists()
    async with stage.locked(platform.backend):
        pass
    assert (stage.root / '.owner').is_file()


async def test_replay_still_requires_current_membership(client, platform, tmp_path):
    from sqlalchemy import update
    from workbench_agent.persistence.schema import team_members

    object.__setattr__(platform.settings.storage, 'upload_staging_root', tmp_path / 'stage')
    data = b'data'
    run = await platform.create_run()
    artifacts = ArtifactService(platform.backend)
    item = await artifacts.begin_upload(
        platform.auth, run.run_id, 'report', sha256(data).hexdigest(), len(data)
    )
    url = f'/v1/uploads/{item["artifact_id"]}/complete'
    assert (
        await client.post(url, content=data, headers={'Idempotency-Key': 'revoked-replay'})
    ).status_code == 202
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(team_members)
            .where(
                team_members.c.team_id == platform.auth.team_id,
                team_members.c.subject == platform.auth.subject,
            )
            .values(status='revoked')
        )
    assert (
        await client.post(url, content=b'ignored', headers={'Idempotency-Key': 'revoked-replay'})
    ).status_code in (403, 404)
