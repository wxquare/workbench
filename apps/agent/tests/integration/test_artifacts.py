import pytest
from uuid import uuid4
from sqlalchemy import insert, update
from workbench_agent.persistence import schema as s
from workbench_agent.application.errors import DomainError


@pytest.mark.asyncio
async def test_artifact_authority_precedes_signing_and_rechecks_on_every_request(platform):
    from workbench_agent.application.artifacts import ArtifactService

    run = await platform.create_run()
    artifact_id = str(uuid4())
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            insert(s.artifacts).values(
                team_id=platform.auth.team_id,
                artifact_id=artifact_id,
                run_id=run.run_id,
                kind='report',
                object_key='opaque/private/report',
                sha256='a' * 64,
                size=10,
                status='available',
            )
        )
    calls = []

    class Signer:
        async def sign(self, key, *, sha256, size, expires_in):
            calls.append(key)
            return 'https://objects.example.test/scoped-report'

    service = ArtifactService(platform.backend, Signer())
    assert (await service.url(platform.auth, artifact_id))[
        'url'
    ] == 'https://objects.example.test/scoped-report'
    assert calls == ['opaque/private/report']
    with pytest.raises(DomainError) as e:
        await service.url(platform.other_auth, artifact_id)
    assert e.value.status == 404
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(s.artifacts)
            .where(s.artifacts.c.artifact_id == artifact_id)
            .values(status='uploading')
        )
    with pytest.raises(DomainError):
        await service.url(platform.auth, artifact_id)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_artifact_old_scope_expiry_and_default_api_fail_closed(platform, client):
    from support.events import evidence

    run = await platform.create_run()
    ref = await evidence(platform, run)
    artifact_id = str(uuid4())
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            insert(s.artifacts).values(
                team_id=platform.auth.team_id,
                artifact_id=artifact_id,
                run_id=run.run_id,
                kind='evidence',
                object_key='opaque/observation',
                sha256='a' * 64,
                size=10,
                status='available',
            )
        )
        await uow.execute(
            update(s.evidence)
            .where(s.evidence.c.evidence_id == ref)
            .values(artifact_ref=artifact_id)
        )
    path = f'/v1/artifacts/{artifact_id}/url'
    assert (await client.get(path)).status_code == 503
    await evidence(platform, run, region='PH')
    assert (await client.get(path)).status_code == 404
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(s.artifacts).where(s.artifacts.c.artifact_id == artifact_id).values(expires_at=0)
        )
    assert (await client.get(path)).status_code == 409


@pytest.mark.asyncio
async def test_revocation_during_signing_does_not_issue_url(platform):
    from workbench_agent.application.artifacts import ArtifactService

    run = await platform.create_run()
    artifact_id = str(uuid4())
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            insert(s.artifacts).values(
                team_id=platform.auth.team_id,
                artifact_id=artifact_id,
                run_id=run.run_id,
                kind='report',
                object_key='opaque/report',
                sha256='b' * 64,
                size=10,
                status='available',
            )
        )

    class RevokingSigner:
        async def sign(self, key, **kwargs):
            async with platform.backend.create_uow().begin_write() as uow:
                await uow.execute(
                    update(s.team_members)
                    .where(s.team_members.c.team_id == platform.auth.team_id)
                    .values(status='revoked')
                )
            return 'https://objects.example.test/report'

    with pytest.raises(DomainError) as error:
        await ArtifactService(platform.backend, RevokingSigner()).url(platform.auth, artifact_id)
    assert error.value.status == 404
