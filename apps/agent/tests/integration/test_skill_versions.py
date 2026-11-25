import io
from hashlib import sha256
from uuid import uuid4
import zipfile
import pytest
from workbench_agent.domain.profiles import VersionRef
from workbench_agent.jobs.worker import CapabilityBlocked
from workbench_agent.skill_management.loader import SkillLoader
from workbench_agent.skill_management.objects import LocalSkillObjects
from workbench_agent.skill_management.validator import validate_bundle
from support.skills import pinned_execution


def make_archive(body):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr(
            'payments/SKILL.md', f'---\nname: payments\ndescription: test\n---\n{body}'
        )
        archive.writestr('payments/references/details.md', body)
    return stream.getvalue()


async def insert_published(platform, raw, objects, version):
    bundle = validate_bundle(raw, 'payments')
    ref = objects.put(raw)
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
                description='test',
                status='active',
                revision=0,
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
                version=version,
                hash=bundle.bundle_hash,
                bundle_ref=ref,
                status='published',
                created_at=now,
                updated_at=now,
            ),
        )
    return VersionRef(resource_id=version_id, version=str(version), content_hash=bundle.bundle_hash)


async def test_loader_reads_pinned_content_and_single_resource_hash(platform, tmp_path):
    objects = LocalSkillObjects(tmp_path / 'objects')
    first = await insert_published(platform, make_archive('first'), objects, 1)
    await insert_published(platform, make_archive('second'), objects, 2)
    execution = await pinned_execution(platform, (first,))
    context = execution.claim.context
    resource = await SkillLoader(platform.backend, objects).load(
        context, first, 'references/details.md', execution=execution
    )
    assert resource.content == 'first'
    assert resource.skill_version_ref == first
    assert resource.content_hash == sha256(b'first').hexdigest()
    assert resource.content_hash != first.content_hash


async def test_loader_refuses_unpinned_or_missing_content(platform, tmp_path):
    objects = LocalSkillObjects(tmp_path / 'objects')
    ref = await insert_published(platform, make_archive('first'), objects, 1)
    execution = await pinned_execution(platform, ())
    context = execution.claim.context
    with pytest.raises(CapabilityBlocked):
        await SkillLoader(platform.backend, objects).load(
            context, ref, 'SKILL.md', execution=execution
        )
    execution = await pinned_execution(platform, (ref,))
    context = execution.claim.context
    for item in (tmp_path / 'objects').iterdir():
        item.unlink()
    with pytest.raises(CapabilityBlocked):
        await SkillLoader(platform.backend, objects).load(
            context, ref, 'SKILL.md', execution=execution
        )


async def test_loader_rejects_tampered_object(platform, tmp_path):
    objects = LocalSkillObjects(tmp_path / 'objects')
    ref = await insert_published(platform, make_archive('first'), objects, 1)
    execution = await pinned_execution(platform, (ref,))
    context = execution.claim.context
    for item in (tmp_path / 'objects').iterdir():
        item.write_bytes(b'tampered')
    with pytest.raises(CapabilityBlocked):
        await SkillLoader(platform.backend, objects).load(
            context, ref, 'SKILL.md', execution=execution
        )


async def test_loader_revalidates_current_membership(platform, tmp_path):
    from workbench_agent.application.errors import DomainError

    objects = LocalSkillObjects(tmp_path / 'objects')
    ref = await insert_published(platform, make_archive('first'), objects, 1)
    execution = await pinned_execution(platform, (ref,))
    context = execution.claim.context
    async with platform.backend.create_uow().begin_read() as uow:
        team = await platform.backend.configuration.one(uow, 'teams', team_id=platform.auth.team_id)
    await platform.teams.set_member(
        platform.auth,
        platform.auth.subject,
        'admin',
        'revoked',
        expected_revision=team['revision'],
        key='skill-revoke',
    )
    with pytest.raises(DomainError) as error:
        await SkillLoader(platform.backend, objects).load(
            context, ref, 'SKILL.md', execution=execution
        )
    assert error.value.code == 'NOT_FOUND'
