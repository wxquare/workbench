"""Durably queue and fence repository snapshots for approved local repositories."""

from dataclasses import asdict
import asyncio
from hashlib import sha256
import json
import re
from uuid import uuid4

from workbench_agent.application.authorization import Authorization
from workbench_agent.application.errors import DomainError
from workbench_agent.application.idempotency import canonical, digest
from workbench_agent.application.profiles import ProfileService
from workbench_agent.domain.admin_jobs import AdminAuth, AdminJob
from workbench_agent.domain.identity import AuthContext
from workbench_agent.jobs.admin import AdminJobGuard
from workbench_agent.execution.snapshots import archive_commit


class RepositoryService:
    def __init__(self, backend):
        self.backend = backend
        self.profiles = ProfileService(backend)

    async def sync(self, auth, agent_id, repo_id, ref, *, key):
        if not isinstance(repo_id, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,127}', repo_id):
            raise DomainError('INVALID_REPOSITORY', 422)
        if not isinstance(ref, str) or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', ref):
            raise DomainError('INVALID_GIT_REF', 422)
        request = {'repo_id': repo_id, 'commit': ref}
        async with self.backend.create_uow().begin_write() as uow:
            await self.profiles.agent(uow, auth, agent_id, 'admin')

            async def operation():
                payload = {
                    **request,
                    'subject': auth.subject,
                    'identity_kind': auth.identity_kind,
                    'agent_scope': auth.agent_scope,
                }
                principal = AdminAuth(auth.team_id, agent_id, auth.subject, True)
                job = await self.backend.admin_jobs.submit(
                    uow, principal, 'repo_sync', canonical(payload), digest(request), key
                )
                return asdict(job)

            result = await self.profiles.idempotency.execute(
                uow, auth, agent_id, 'repository.sync', key, request, operation, status=202
            )
            return AdminJob(**result.body)


class RepositorySyncWorker:
    def __init__(self, backend, objects, repositories):
        self.backend, self.objects, self.repositories = backend, objects, dict(repositories)

    async def _check(self, uow, claim, auth):
        await AdminJobGuard(self.backend.admin_jobs).validate(uow, claim)
        await Authorization(self.backend).current(uow, auth, claim.agent_id, 'admin')

    async def execute(self, claim):
        payload = json.loads(claim.payload)
        auth = AuthContext(
            team_id=claim.team_id,
            subject=payload['subject'],
            roles=(),
            identity_kind=payload['identity_kind'],
            agent_scope=payload['agent_scope'],
        )
        repository = self.repositories.get((claim.team_id, claim.agent_id, payload['repo_id']))
        if repository is None:
            raise ValueError('repository not approved')
        async with self.backend.create_uow().begin_write() as uow:
            await self._check(uow, claim, auth)
        archive = await archive_commit(repository, payload['commit'])
        checksum = sha256(archive).hexdigest()
        if hasattr(self.objects, 'put_verified'):
            object_ref = await self.objects.put_verified(archive, checksum)
        else:
            object_ref = await asyncio.to_thread(self.objects.put, archive)
        async with self.backend.create_uow().begin_write() as uow:
            await self._check(uow, claim, auth)
            scope = dict(
                team_id=claim.team_id,
                agent_id=claim.agent_id,
                repo_id=payload['repo_id'],
                commit_sha=payload['commit'],
                format_version='tar.v1',
            )
            existing = await self.backend.configuration.one(uow, 'repo_snapshots', **scope)
            if existing:
                if existing['storage_ref'] != object_ref or existing['status'] != 'ready':
                    raise ValueError('snapshot conflicts with existing identity')
                snapshot_id = existing['snapshot_id']
            else:
                snapshot_id = str(uuid4())
                now = await self.backend.clock.now_ms(uow)
                await self.backend.configuration.insert(
                    uow,
                    'repo_snapshots',
                    dict(
                        **scope,
                        snapshot_id=snapshot_id,
                        storage_ref=object_ref,
                        status='ready',
                        created_at=now,
                        updated_at=now,
                    ),
                )
        return canonical(
            {'snapshot_id': snapshot_id, 'commit': payload['commit'], 'sha256': checksum}
        )
