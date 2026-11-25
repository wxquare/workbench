from uuid import uuid4
import pytest
from workbench_agent.application.errors import DomainError
from workbench_agent.domain.profiles import ProfileDraft
from workbench_agent.jobs.worker import CapabilityBlocked, ExecutorRegistry, Worker
from workbench_agent.skill_management.importer import SkillImporter, SkillImportWorker
from workbench_agent.skill_management.objects import LocalSkillObjects
from workbench_agent.skill_management.versions import SkillService
from workbench_agent.skill_management.loader import SkillLoader
from support.skills import pinned_execution, execution_for_run


async def test_seed_import_job_publish_and_frozen_run_context(platform, tmp_path):
    root = tmp_path / 'assets'
    skill = root / 'skills' / 'payments'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('---\nname: payments\ndescription: payments\n---\nversion one')
    objects = LocalSkillObjects(tmp_path / 'objects')
    importer = SkillImporter(platform.backend)
    job = await importer.import_seed(platform.auth, platform.agent_id, 'payments', key='import-one')
    replay = await importer.import_seed(
        platform.auth, platform.agent_id, 'payments', key='import-one'
    )
    assert job.job_id == replay.job_id
    registry = ExecutorRegistry()
    registry.register_admin(
        'skill_import', SkillImportWorker(platform.backend, objects, root).execute
    )
    assert (
        await Worker(platform.backend, 'skill-worker', registry).run_admin_once()
        == 'admin_completed'
    )
    service = SkillService(platform.backend, objects)
    ref = await service.publish(
        platform.auth, platform.agent_id, job.job_id, expected_revision=0, key='publish-one'
    )
    again = await service.publish(
        platform.auth, platform.agent_id, job.job_id, expected_revision=0, key='publish-one'
    )
    assert again == ref
    agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    async with platform.backend.create_uow().begin_read() as uow:
        old = await platform.profiles.published(uow, platform.auth, platform.agent_id)
    draft = ProfileDraft(
        default_model=old.default_model, graph_id='conversation.multi_turn', skills=(ref,)
    )
    updated = await platform.profiles.save_draft(
        platform.auth, platform.agent_id, draft, agent['revision'], key=str(uuid4())
    )
    await platform.profiles.publish(
        platform.auth, platform.agent_id, updated['revision'], key=str(uuid4())
    )
    run = await platform.create_run(key='first-import-run')
    execution = await execution_for_run(platform, run)
    context = execution.claim.context
    (skill / 'SKILL.md').write_text(
        '---\nname: payments\ndescription: payments\n---\nchanged source'
    )
    resource = await SkillLoader(platform.backend, objects).load(
        context, ref, 'SKILL.md', execution=execution
    )
    assert resource.content.endswith('version one')
    assert resource.skill_version_ref == ref


async def test_seed_import_refuses_symlink(platform, tmp_path):
    root = tmp_path / 'assets'
    skill = root / 'skills' / 'payments'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('---\nname: payments\ndescription: payments\n---\nbody')
    (skill / 'escape').symlink_to('/etc/passwd')
    objects = LocalSkillObjects(tmp_path / 'objects')
    job = await SkillImporter(platform.backend).import_seed(
        platform.auth, platform.agent_id, 'payments', key='import-bad'
    )
    registry = ExecutorRegistry()
    registry.register_admin(
        'skill_import', SkillImportWorker(platform.backend, objects, root).execute
    )
    assert (
        await Worker(platform.backend, 'skill-worker', registry).run_admin_once() == 'admin_blocked'
    )
    with pytest.raises(DomainError, match='SKILL_IMPORT_UNAVAILABLE'):
        await SkillService(platform.backend, objects).publish(
            platform.auth, platform.agent_id, job.job_id, expected_revision=0, key='publish-bad'
        )


async def test_http_import_is_durable_and_does_not_read_seed(client, platform):
    url = f'/v1/agents/{platform.agent_id}/skills/payments/import'
    response = await client.post(url, headers={'Idempotency-Key': 'http-import'})
    assert response.status_code == 202
    assert response.json()['kind'] == 'skill_import'
    replay = await client.post(url, headers={'Idempotency-Key': 'http-import'})
    assert replay.json()['job_id'] == response.json()['job_id']


async def test_authorized_artifact_import_checks_digest_and_agent(platform, tmp_path):
    from hashlib import sha256
    from sqlalchemy import insert
    from workbench_agent.persistence.schema import artifacts
    from io import BytesIO
    import zipfile

    objects = LocalSkillObjects(tmp_path / 'objects')
    archive = BytesIO()
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr(
            'payments/SKILL.md', '---\nname: payments\ndescription: test\n---\nartifact source'
        )
    raw = archive.getvalue()
    object_ref = objects.put(raw)
    run = await platform.create_run(key='artifact-run')
    artifact_id = str(uuid4())
    async with platform.backend.create_uow().begin_write() as uow:
        now = await platform.backend.clock.now_ms(uow)
        await uow.execute(
            insert(artifacts).values(
                team_id=platform.auth.team_id,
                artifact_id=artifact_id,
                run_id=run.run_id,
                kind='skill_bundle',
                object_key=object_ref,
                sha256=sha256(raw).hexdigest(),
                size=len(raw),
                status='available',
                created_at=now,
                updated_at=now,
            )
        )
    importer = SkillImporter(platform.backend)
    job = await importer.import_artifact(
        platform.auth, platform.agent_id, 'payments', artifact_id, key='artifact-import'
    )
    registry = ExecutorRegistry()
    registry.register_admin(
        'skill_import', SkillImportWorker(platform.backend, objects, tmp_path).execute
    )
    assert (
        await Worker(platform.backend, 'artifact-worker', registry).run_admin_once()
        == 'admin_completed'
    )
    ref = await SkillService(platform.backend, objects).publish(
        platform.auth, platform.agent_id, job.job_id, expected_revision=0, key='artifact-publish'
    )
    assert ref.content_hash
    with pytest.raises(DomainError):
        await importer.import_artifact(
            platform.other_auth, platform.agent_id, 'payments', artifact_id, key='foreign'
        )


async def test_git_import_reads_allowlisted_fixed_commit(platform, tmp_path):
    import subprocess

    repository = tmp_path / 'repository'
    repository.mkdir()

    def git(*args):
        return subprocess.run(
            ['git', '-C', str(repository), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git('init', '-q')
    skill = repository / 'skills' / 'payments'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('---\nname: payments\ndescription: git\n---\noriginal')
    git('add', 'skills/payments/SKILL.md')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'seed')
    commit = git('rev-parse', 'HEAD')
    (skill / 'SKILL.md').write_text('---\nname: payments\ndescription: git\n---\nuncommitted')
    objects = LocalSkillObjects(tmp_path / 'objects')
    importer = SkillImporter(platform.backend)
    job = await importer.import_git(
        platform.auth, platform.agent_id, 'payments', 'safe-repo', commit, key='git-import'
    )
    registry = ExecutorRegistry()
    registry.register_admin(
        'skill_import',
        SkillImportWorker(
            platform.backend, objects, tmp_path, git_repositories={'safe-repo': repository}
        ).execute,
    )
    assert (
        await Worker(platform.backend, 'git-worker', registry).run_admin_once() == 'admin_completed'
    )
    ref = await SkillService(platform.backend, objects).publish(
        platform.auth, platform.agent_id, job.job_id, expected_revision=0, key='git-publish'
    )
    execution = await pinned_execution(platform, (ref,))
    context = execution.claim.context
    assert (
        await SkillLoader(platform.backend, objects).load(
            context, ref, 'SKILL.md', execution=execution
        )
    ).content.endswith('original')
    with pytest.raises(DomainError):
        await importer.import_git(
            platform.auth, platform.agent_id, 'payments', 'safe-repo', 'HEAD', key='git-moving'
        )


async def test_git_import_rejects_oversized_decompressed_file(platform, tmp_path, monkeypatch):
    import subprocess

    repository = tmp_path / 'large-repository'
    repository.mkdir()

    def git(*args):
        return subprocess.run(
            ['git', '-C', str(repository), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git('init', '-q')
    skill = repository / 'skills' / 'payments'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('---\nname: payments\ndescription: git\n---\noriginal')
    (skill / 'oversized.md').write_bytes(b'x' * (2 * 1024 * 1024 + 1))
    git('add', 'skills')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'seed')
    commit = git('rev-parse', 'HEAD')
    import zipfile
    from workbench_agent.skill_management.importer import _read_git

    original = zipfile.ZipFile.read

    def guarded_read(archive, entry, *args, **kwargs):
        assert entry.file_size <= 2 * 1024 * 1024, 'oversized entry decompressed'
        return original(archive, entry, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, 'read', guarded_read)
    with pytest.raises(ValueError):
        await _read_git(repository, commit, 'payments')


async def test_real_run_keeps_skill_across_rollout_and_agent_rollback(platform, tmp_path):
    root = tmp_path / 'assets'
    skill = root / 'skills' / 'payments'
    skill.mkdir(parents=True)
    doc = skill / 'SKILL.md'
    objects = LocalSkillObjects(tmp_path / 'objects')
    registry = ExecutorRegistry()
    registry.register_admin(
        'skill_import', SkillImportWorker(platform.backend, objects, root).execute
    )
    worker = Worker(platform.backend, 'version-worker', registry)
    importer, service = SkillImporter(platform.backend), SkillService(platform.backend, objects)

    async def publish(content, number):
        doc.write_text(f'---\nname: payments\ndescription: payments\n---\n{content}')
        job = await importer.import_seed(
            platform.auth, platform.agent_id, 'payments', key=f'import-{number}'
        )
        assert await worker.run_admin_once() == 'admin_completed'
        return await service.publish(
            platform.auth,
            platform.agent_id,
            job.job_id,
            expected_revision=number - 1,
            key=f'publish-{number}',
        )

    async def bind(ref):
        agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
        async with platform.backend.create_uow().begin_read() as uow:
            current = await platform.profiles.published(uow, platform.auth, platform.agent_id)
        draft = ProfileDraft(
            default_model=current.default_model, graph_id='conversation.multi_turn', skills=(ref,)
        )
        changed = await platform.profiles.save_draft(
            platform.auth, platform.agent_id, draft, agent['revision'], key=str(uuid4())
        )
        await platform.profiles.publish(
            platform.auth, platform.agent_id, changed['revision'], key=str(uuid4())
        )

    first = await publish('version one', 1)
    await bind(first)
    run = await platform.create_run(key='pinned-run')
    second = await publish('version two', 2)
    await bind(second)
    execution = await execution_for_run(platform, run)
    old_context = execution.claim.context
    loader = SkillLoader(platform.backend, objects)
    assert (
        await loader.load(old_context, first, 'SKILL.md', execution=execution)
    ).content.endswith('version one')
    with pytest.raises(CapabilityBlocked):
        await loader.load(old_context, second, 'SKILL.md', execution=execution)
    await bind(first)
    rolled_back = await platform.contexts.resolve(platform.auth, platform.agent_id)
    assert rolled_back.skills == (first,)


async def test_import_job_rechecks_admin_after_revocation(platform, tmp_path):
    root = tmp_path / 'assets'
    skill = root / 'skills' / 'payments'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('---\nname: payments\ndescription: payments\n---\nbody')
    objects = LocalSkillObjects(tmp_path / 'objects')
    await SkillImporter(platform.backend).import_seed(
        platform.auth, platform.agent_id, 'payments', key='pending-revoke'
    )
    await platform.teams.set_member(
        platform.auth,
        platform.auth.subject,
        'admin',
        'revoked',
        expected_revision=0,
        key='skill-admin-revoke',
    )
    registry = ExecutorRegistry()
    registry.register_admin(
        'skill_import', SkillImportWorker(platform.backend, objects, root).execute
    )
    assert (
        await Worker(platform.backend, 'revoked-worker', registry).run_admin_once()
        == 'admin_blocked'
    )
    assert list((tmp_path / 'objects').iterdir()) == []


async def test_publish_revision_conflict_and_idempotency_are_audited(platform, tmp_path):
    from sqlalchemy import select
    from workbench_agent.persistence.schema import audit_logs

    root = tmp_path / 'assets'
    skill = root / 'skills' / 'payments'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('---\nname: payments\ndescription: payments\n---\nbody')
    objects = LocalSkillObjects(tmp_path / 'objects')
    job = await SkillImporter(platform.backend).import_seed(
        platform.auth, platform.agent_id, 'payments', key='audit-import'
    )
    registry = ExecutorRegistry()
    registry.register_admin(
        'skill_import', SkillImportWorker(platform.backend, objects, root).execute
    )
    assert (
        await Worker(platform.backend, 'audit-worker', registry).run_admin_once()
        == 'admin_completed'
    )
    service = SkillService(platform.backend, objects)
    ref = await service.publish(
        platform.auth, platform.agent_id, job.job_id, expected_revision=0, key='audit-publish'
    )
    assert ref == await service.publish(
        platform.auth, platform.agent_id, job.job_id, expected_revision=0, key='audit-publish'
    )
    with pytest.raises(DomainError, match='IDEMPOTENCY_CONFLICT'):
        await service.publish(
            platform.auth, platform.agent_id, job.job_id, expected_revision=1, key='audit-publish'
        )
    with pytest.raises(DomainError, match='REVISION_CONFLICT'):
        await service.publish(
            platform.auth,
            platform.agent_id,
            job.job_id,
            expected_revision=0,
            key='different-publish',
        )
    async with platform.backend.create_uow().begin_read() as uow:
        rows = (
            (
                await uow.execute(
                    select(audit_logs.c.operation).where(
                        audit_logs.c.team_id == platform.auth.team_id,
                        audit_logs.c.operation.in_(['skill.import', 'skill.publish']),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows.count('skill.import') == 1
    assert rows.count('skill.publish') == 1


async def test_artifact_import_replays_after_artifact_expiration(platform, tmp_path):
    from hashlib import sha256
    from io import BytesIO
    import zipfile
    from sqlalchemy import insert, update
    from workbench_agent.persistence.schema import artifacts

    raw = BytesIO()
    with zipfile.ZipFile(raw, 'w') as archive:
        archive.writestr('payments/SKILL.md', '---\nname: payments\ndescription: test\n---\nbody')
    data = raw.getvalue()
    objects = LocalSkillObjects(tmp_path / 'objects')
    object_ref = objects.put(data)
    run = await platform.create_run(key='replay-artifact-run')
    artifact_id = str(uuid4())
    async with platform.backend.create_uow().begin_write() as uow:
        now = await platform.backend.clock.now_ms(uow)
        await uow.execute(
            insert(artifacts).values(
                team_id=platform.auth.team_id,
                artifact_id=artifact_id,
                run_id=run.run_id,
                kind='skill_bundle',
                object_key=object_ref,
                sha256=sha256(data).hexdigest(),
                size=len(data),
                status='available',
                created_at=now,
                updated_at=now,
            )
        )
    importer = SkillImporter(platform.backend)
    job = await importer.import_artifact(
        platform.auth, platform.agent_id, 'payments', artifact_id, key='expired-replay'
    )
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(artifacts).where(artifacts.c.artifact_id == artifact_id).values(expires_at=1)
        )
    assert (
        await importer.import_artifact(
            platform.auth, platform.agent_id, 'payments', artifact_id, key='expired-replay'
        )
    ).job_id == job.job_id
    with pytest.raises(DomainError, match='IDEMPOTENCY_CONFLICT'):
        await importer.import_artifact(
            platform.auth, platform.agent_id, 'other', artifact_id, key='expired-replay'
        )


async def test_publish_replays_if_original_object_becomes_unavailable(platform, tmp_path):
    root = tmp_path / 'assets'
    skill = root / 'skills' / 'payments'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('---\nname: payments\ndescription: test\n---\nbody')
    objects = LocalSkillObjects(tmp_path / 'objects')
    job = await SkillImporter(platform.backend).import_seed(
        platform.auth, platform.agent_id, 'payments', key='object-replay-import'
    )
    registry = ExecutorRegistry()
    registry.register_admin(
        'skill_import', SkillImportWorker(platform.backend, objects, root).execute
    )
    assert (
        await Worker(platform.backend, 'replay-worker', registry).run_admin_once()
        == 'admin_completed'
    )
    service = SkillService(platform.backend, objects)
    ref = await service.publish(
        platform.auth, platform.agent_id, job.job_id, expected_revision=0, key='object-replay'
    )
    for item in objects.root.iterdir():
        item.unlink()
    assert (
        await service.publish(
            platform.auth, platform.agent_id, job.job_id, expected_revision=0, key='object-replay'
        )
        == ref
    )
    with pytest.raises(DomainError, match='IDEMPOTENCY_CONFLICT'):
        await service.publish(
            platform.auth, platform.agent_id, job.job_id, expected_revision=1, key='object-replay'
        )
