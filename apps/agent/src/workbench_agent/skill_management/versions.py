"""Publish a validated management-job result as an immutable skill version."""

import json
from uuid import uuid4
from workbench_agent.application.errors import DomainError
from workbench_agent.application.profiles import ProfileService
from workbench_agent.domain.admin_jobs import AdminAuth
from workbench_agent.domain.profiles import VersionRef
from workbench_agent.skill_management.validator import validate_bundle


class SkillService:
    def __init__(self, backend, objects):
        self.backend, self.objects = backend, objects
        self.profiles = ProfileService(backend)

    async def publish(self, auth, agent_id, job_id, *, expected_revision, key, expected_name=None):
        principal = AdminAuth(auth.team_id, agent_id, auth.subject, True)
        payload = {
            'job_id': job_id,
            'expected_revision': expected_revision,
            'expected_name': expected_name,
        }
        async with self.backend.create_uow().begin_read() as uow:
            await self.profiles.agent(uow, auth, agent_id, 'admin')
            replay = await self.profiles.idempotency.lookup(
                uow, auth, agent_id, 'skill.publish', key, payload
            )
            if replay is not None:
                return VersionRef.model_validate(replay.body)
            job = await self.backend.admin_jobs.get(uow, principal, job_id)
            if (
                not job
                or job.kind != 'skill_import'
                or job.status != 'succeeded'
                or not job.result_ref
            ):
                raise DomainError('SKILL_IMPORT_UNAVAILABLE', 409)
            try:
                candidate = json.loads(job.result_ref)
            except (TypeError, ValueError):
                raise DomainError('SKILL_IMPORT_UNAVAILABLE', 409) from None
        try:
            from workbench_agent.skill_management.manifest import (
                PREFIX,
                read_manifest,
                read_resource,
                validate_skill_metadata,
            )

            if candidate['bundle_ref'].startswith(PREFIX):
                manifest = read_manifest(
                    self.objects, candidate['bundle_ref'], candidate['name'], candidate['hash']
                )
                contents = {
                    path: read_resource(self.objects, manifest, path) for path in manifest['hashes']
                }
                validate_skill_metadata(manifest, contents['SKILL.md'])
                # Reapply all import restrictions, including executable resources.
                from io import BytesIO
                import zipfile

                output = BytesIO()
                with zipfile.ZipFile(output, 'w') as archive:
                    for path, content in contents.items():
                        archive.writestr(candidate['name'] + '/' + path, content)
                bundle = validate_bundle(output.getvalue(), candidate['name'])
            else:
                bundle = validate_bundle(
                    self.objects.get(candidate['bundle_ref']), candidate['name']
                )
            if (
                bundle.bundle_hash != candidate['hash']
                or bundle.description != candidate['description']
                or expected_name is not None
                and bundle.name != expected_name
            ):
                raise ValueError()
        except (OSError, ValueError, KeyError):
            raise DomainError('SKILL_IMPORT_UNAVAILABLE', 409) from None
        async with self.backend.create_uow().begin_write() as uow:
            await self.profiles.agent(uow, auth, agent_id, 'admin')

            async def operation():
                current = await self.backend.admin_jobs.get(uow, principal, job_id)
                if current != job:
                    raise DomainError('SKILL_IMPORT_UNAVAILABLE', 409)
                skill = await self.backend.configuration.one(
                    uow, 'skills', team_id=auth.team_id, agent_id=agent_id, name=bundle.name
                )
                if skill is None:
                    if expected_revision != 0:
                        raise DomainError('REVISION_CONFLICT', 409)
                    now = await self.backend.clock.now_ms(uow)
                    skill_id = str(uuid4())
                    await self.backend.configuration.insert(
                        uow,
                        'skills',
                        dict(
                            team_id=auth.team_id,
                            agent_id=agent_id,
                            skill_id=skill_id,
                            name=bundle.name,
                            description=bundle.description,
                            status='active',
                            revision=0,
                            created_at=now,
                            updated_at=now,
                        ),
                    )
                else:
                    skill_id = skill['skill_id']
                    if skill['revision'] != expected_revision:
                        raise DomainError('REVISION_CONFLICT', 409)
                version = expected_revision + 1
                now = await self.backend.clock.now_ms(uow)
                ident = str(uuid4())
                await self.backend.configuration.insert(
                    uow,
                    'skill_versions',
                    dict(
                        team_id=auth.team_id,
                        skill_version_id=ident,
                        skill_id=skill_id,
                        version=version,
                        hash=bundle.bundle_hash,
                        bundle_ref=candidate['bundle_ref'],
                        status='published',
                        created_at=now,
                        updated_at=now,
                    ),
                )
                changed = await self.backend.configuration.change(
                    uow,
                    'skills',
                    {'team_id': auth.team_id, 'skill_id': skill_id, 'revision': expected_revision},
                    {'revision': version, 'description': bundle.description, 'updated_at': now},
                )
                if not changed:
                    raise DomainError('REVISION_CONFLICT', 409)
                return VersionRef(
                    resource_id=ident, version=str(version), content_hash=bundle.bundle_hash
                ).model_dump()

            result = await self.profiles.idempotency.execute(
                uow, auth, agent_id, 'skill.publish', key, payload, operation, status=201
            )
            return VersionRef.model_validate(result.body)
