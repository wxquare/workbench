import hashlib
from concurrent.futures import ThreadPoolExecutor
import pytest
from workbench_agent.skill_management.objects import LocalSkillObjects
from workbench_agent.skill_management.validator import InvalidBundle, validate_bundle
from workbench_agent.skill_management.loader import SkillLoader
from workbench_agent.jobs.worker import CapabilityBlocked
from test_skill_versions import make_archive, insert_published
from support.skills import execution_for_run


def test_object_publish_does_not_expose_partial_on_interruption(tmp_path, monkeypatch):
    import os

    objects = LocalSkillObjects(tmp_path / 'objects')
    content = b'complete object'
    target = objects.root / hashlib.sha256(content).hexdigest()

    def crash(*args, **kwargs):
        assert not target.exists()
        raise OSError('simulated interrupted publish')

    monkeypatch.setattr(os, 'link', crash)
    with pytest.raises(OSError):
        objects.put(content)
    assert not target.exists()


def test_concurrent_same_content_publication_has_one_complete_object(tmp_path):
    objects = LocalSkillObjects(tmp_path / 'objects')
    payload = b'x' * 1_000_000
    with ThreadPoolExecutor(max_workers=8) as pool:
        refs = list(pool.map(objects.put, [payload] * 8))
    assert len(set(refs)) == 1
    assert objects.get(refs[0]) == payload
    assert len(list(objects.root.iterdir())) == 1


def test_existing_corrupt_object_not_overwritten(tmp_path):
    objects = LocalSkillObjects(tmp_path / 'objects')
    payload = b'complete'
    path = objects.root / hashlib.sha256(payload).hexdigest()
    path.write_bytes(b'corrupt')
    with pytest.raises(ValueError):
        objects.put(payload)
    assert path.read_bytes() == b'corrupt'


def test_binary_resource_rejected_at_import():
    import io
    import zipfile

    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr('payments/SKILL.md', '---\nname: payments\ndescription: test\n---\ntext')
        archive.writestr('payments/references/image.dat', b'\xff\x00\x01')
    with pytest.raises(InvalidBundle):
        validate_bundle(data.getvalue(), 'payments')


async def test_forged_frozen_context_cannot_load_unpinned_skill(platform, tmp_path):
    objects = LocalSkillObjects(tmp_path / 'objects')
    ref = await insert_published(platform, make_archive('private'), objects, 1)
    run = await platform.create_run(key='unpinned-run')
    context = (await platform.get_run(run.run_id)).context
    execution = await execution_for_run(platform, run)
    forged = context.model_copy(update={'skills': (ref,)})
    with pytest.raises(CapabilityBlocked):
        await SkillLoader(platform.backend, objects).load(
            forged, ref, 'SKILL.md', execution=execution
        )


def test_readers_cannot_see_object_before_complete_publication(tmp_path, monkeypatch):
    import os
    from threading import Event

    objects = LocalSkillObjects(tmp_path / 'objects')
    payload = b'bounded' * 100000
    reference = 'sha256:' + hashlib.sha256(payload).hexdigest()
    entered, release = Event(), Event()
    original = os.fsync

    def pause(fd):
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        return original(fd)

    monkeypatch.setattr(os, 'fsync', pause)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(objects.put, payload)
        try:
            assert entered.wait(5)
            with pytest.raises(FileNotFoundError):
                objects.get(reference)
        finally:
            release.set()
        assert pending.result() == reference
    assert objects.get(reference) == payload


async def test_loader_rejects_cross_tenant_and_inactive_worker(platform, tmp_path):
    from support.skills import pinned_execution

    objects = LocalSkillObjects(tmp_path / 'objects')
    ref = await insert_published(platform, make_archive('private'), objects, 1)
    execution = await pinned_execution(platform, (ref,))
    context = execution.claim.context
    loader = SkillLoader(platform.backend, objects)
    forged = context.model_copy(
        update={'team_id': platform.other_auth.team_id, 'subject': platform.other_auth.subject}
    )
    with pytest.raises(CapabilityBlocked):
        await loader.load(forged, ref, 'SKILL.md', execution=execution)
    execution._tool_owner._tool_executions.clear()
    with pytest.raises(CapabilityBlocked):
        await loader.load(context, ref, 'SKILL.md', execution=execution)


async def test_legacy_binary_bundle_keeps_text_and_binary_resources_loadable(platform, tmp_path):
    import io
    import json
    import zipfile
    from uuid import uuid4
    from workbench_agent.domain.profiles import VersionRef
    from support.skills import pinned_execution

    text = b'---\nname: payments\ndescription: legacy\n---\nlegacy text'
    binary = b'\xff\x00\x01'
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, 'w') as archive:
        archive.writestr('payments/SKILL.md', text)
        archive.writestr('payments/references/image.dat', binary)
    manifest = {
        'SKILL.md': hashlib.sha256(text).hexdigest(),
        'references/image.dat': hashlib.sha256(binary).hexdigest(),
    }
    bundle_hash = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()
    ).hexdigest()
    objects = LocalSkillObjects(tmp_path / 'objects')
    bundle_ref = objects.put(raw.getvalue())
    skill_id, version_id = str(uuid4()), str(uuid4())
    async with platform.backend.create_uow().begin_write() as uow:
        now = await platform.backend.clock.now_ms(uow)
        await platform.backend.configuration.insert(
            uow,
            'skills',
            dict(
                team_id=platform.auth.team_id,
                agent_id=platform.agent_id,
                skill_id=skill_id,
                name='payments',
                description='legacy',
                status='active',
                revision=1,
                created_at=now,
                updated_at=now,
            ),
        )
        await platform.backend.configuration.insert(
            uow,
            'skill_versions',
            dict(
                team_id=platform.auth.team_id,
                skill_version_id=version_id,
                skill_id=skill_id,
                version=1,
                hash=bundle_hash,
                bundle_ref=bundle_ref,
                status='published',
                created_at=now,
                updated_at=now,
            ),
        )
    ref = VersionRef(resource_id=version_id, version='1', content_hash=bundle_hash)
    execution = await pinned_execution(platform, (ref,))
    loader = SkillLoader(platform.backend, objects)
    restored = await loader.load(execution.claim.context, ref, 'SKILL.md', execution=execution)
    assert restored.content == text.decode('utf-8')
    assert restored.content_hash == hashlib.sha256(text).hexdigest()
    image = await loader.load(
        execution.claim.context, ref, 'references/image.dat', execution=execution
    )
    assert image.content == binary
    assert image.content_hash == hashlib.sha256(binary).hexdigest()
    with pytest.raises(InvalidBundle, match='binary'):
        validate_bundle(raw.getvalue(), 'payments')


async def test_new_skill_reads_only_manifest_skill_and_requested_resource(platform, tmp_path):
    from support.skills import pinned_execution
    from workbench_agent.jobs.worker import ExecutorRegistry, Worker
    from workbench_agent.skill_management.importer import SkillImporter, SkillImportWorker
    from workbench_agent.skill_management.versions import SkillService

    root = tmp_path / 'assets'
    skill = root / 'skills' / 'payments'
    (skill / 'references').mkdir(parents=True)
    (skill / 'SKILL.md').write_text('---\nname: payments\ndescription: test\n---\ninstructions')
    (skill / 'references' / 'wanted.md').write_text('wanted')
    (skill / 'references' / 'unused.md').write_text('unused')

    class TrackingObjects(LocalSkillObjects):
        def __init__(self, path):
            super().__init__(path)
            self.reads = []

        def get(self, ref):
            self.reads.append(ref)
            return super().get(ref)

    objects = TrackingObjects(tmp_path / 'objects')
    job = await SkillImporter(platform.backend).import_seed(
        platform.auth, platform.agent_id, 'payments', key='lazy-import'
    )
    registry = ExecutorRegistry()
    registry.register_admin(
        'skill_import', SkillImportWorker(platform.backend, objects, root).execute
    )
    assert (
        await Worker(platform.backend, 'import-worker', registry).run_admin_once()
        == 'admin_completed'
    )
    ref = await SkillService(platform.backend, objects).publish(
        platform.auth, platform.agent_id, job.job_id, expected_revision=0, key='lazy-publish'
    )
    execution = await pinned_execution(platform, (ref,))
    objects.reads.clear()
    resource = await SkillLoader(platform.backend, objects).load(
        execution.claim.context, ref, 'references/wanted.md', execution=execution
    )
    assert resource.content == 'wanted'
    assert resource.content_hash == hashlib.sha256(b'wanted').hexdigest()
    assert len(objects.reads) == 3
    assert 'sha256:' + hashlib.sha256(b'unused').hexdigest() not in objects.reads
    assert 'sha256:' + hashlib.sha256(b'wanted').hexdigest() in objects.reads


async def test_lazy_load_ignores_missing_unselected_and_rejects_missing_selected(
    platform, tmp_path
):
    from support.skills import pinned_execution
    from workbench_agent.jobs.worker import ExecutorRegistry, Worker
    from workbench_agent.skill_management.importer import SkillImporter, SkillImportWorker
    from workbench_agent.skill_management.versions import SkillService

    root = tmp_path / 'assets'
    skill = root / 'skills' / 'payments'
    (skill / 'references').mkdir(parents=True)
    (skill / 'SKILL.md').write_text('---\nname: payments\ndescription: test\n---\ninstructions')
    (skill / 'references' / 'wanted.md').write_text('wanted')
    (skill / 'references' / 'unused.md').write_text('unused')
    objects = LocalSkillObjects(tmp_path / 'objects')
    job = await SkillImporter(platform.backend).import_seed(
        platform.auth, platform.agent_id, 'payments', key='missing-import'
    )
    registry = ExecutorRegistry()
    registry.register_admin(
        'skill_import', SkillImportWorker(platform.backend, objects, root).execute
    )
    assert (
        await Worker(platform.backend, 'import-worker', registry).run_admin_once()
        == 'admin_completed'
    )
    ref = await SkillService(platform.backend, objects).publish(
        platform.auth, platform.agent_id, job.job_id, expected_revision=0, key='missing-publish'
    )
    execution = await pinned_execution(platform, (ref,))
    loader = SkillLoader(platform.backend, objects)
    (objects.root / hashlib.sha256(b'unused').hexdigest()).unlink()
    resource = await loader.load(
        execution.claim.context, ref, 'references/wanted.md', execution=execution
    )
    assert resource.content == 'wanted'
    (objects.root / hashlib.sha256(b'wanted').hexdigest()).unlink()
    with pytest.raises(CapabilityBlocked):
        await loader.load(execution.claim.context, ref, 'references/wanted.md', execution=execution)
