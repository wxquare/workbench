from hashlib import sha256
import pytest
from workbench_agent.application.artifacts import ArtifactService
from workbench_agent.application.errors import DomainError
from workbench_agent.skill_management.objects import LocalSkillObjects


class Store:
    def __init__(self, path): self.objects = LocalSkillObjects(path)
    async def put_verified(self, data, digest):
        assert sha256(data).hexdigest() == digest
        return self.objects.put(data)


async def test_upload_is_unavailable_until_server_verifies_bytes(platform, tmp_path):
    run = await platform.create_run(); content = b'validated data'; digest = sha256(content).hexdigest()
    service = ArtifactService(platform.backend, objects=Store(tmp_path / 'objects'))
    uploading = await service.begin_upload(platform.auth, run.run_id, 'report', digest, len(content))
    assert uploading['status'] == 'uploading'
    with pytest.raises(DomainError):
        await service._authorized(platform.auth, uploading['artifact_id'])
    with pytest.raises(DomainError):
        await service.complete(platform.auth, uploading['artifact_id'], b'wrong')
    assert (await service.complete(platform.auth, uploading['artifact_id'], content))['status'] == 'available'
    assert (await service._authorized(platform.auth, uploading['artifact_id']))['sha256'] == digest


async def test_upload_foreign_user_and_changed_bytes_are_denied(platform, tmp_path):
    run = await platform.create_run(); data = b'data'; digest = sha256(data).hexdigest()
    service = ArtifactService(platform.backend, objects=Store(tmp_path / 'objects'))
    item = await service.begin_upload(platform.auth, run.run_id, 'report', digest, len(data))
    with pytest.raises(DomainError):
        await service.complete(platform.other_auth, item['artifact_id'], data)
    assert (await service.complete(platform.auth, item['artifact_id'], data))['status'] == 'available'
    with pytest.raises(DomainError):
        await service.complete(platform.auth, item['artifact_id'], b'other')


async def test_upload_revocation_during_storage_never_makes_artifact_available(platform, tmp_path):
    from sqlalchemy import update
    from workbench_agent.persistence.schema import team_members
    data = b'data'; digest = sha256(data).hexdigest(); run = await platform.create_run()
    class RevokingStore(Store):
        async def put_verified(self, payload, checksum):
            result = await super().put_verified(payload, checksum)
            async with platform.backend.create_uow().begin_write() as uow:
                await uow.execute(update(team_members).where(team_members.c.team_id == platform.auth.team_id,
                    team_members.c.subject == platform.auth.subject).values(status='revoked'))
            return result
    service = ArtifactService(platform.backend, objects=RevokingStore(tmp_path / 'objects'))
    item = await service.begin_upload(platform.auth, run.run_id, 'report', digest, len(data))
    with pytest.raises(DomainError):
        await service.complete(platform.auth, item['artifact_id'], data)
    async with platform.backend.create_uow().begin_read() as uow:
        stored = await platform.backend.results.artifact(uow, platform.auth.team_id, item['artifact_id'])
        assert stored['status'] == 'uploading'


async def test_upload_scope_change_denies_completion(platform, tmp_path):
    from support.events import evidence
    data = b'data'; digest = sha256(data).hexdigest(); run = await platform.create_run()
    service = ArtifactService(platform.backend, objects=Store(tmp_path / 'objects'))
    item = await service.begin_upload(platform.auth, run.run_id, 'report', digest, len(data))
    await evidence(platform, run, region='PH')
    with pytest.raises(DomainError):
        await service.complete(platform.auth, item['artifact_id'], data)
