"""Versioned model profile administration; credentials remain references."""

import json
from uuid import uuid4
from pydantic import ValidationError
from workbench_agent.application.errors import DomainError, not_found
from workbench_agent.application.idempotency import canonical, digest
from workbench_agent.application.profiles import ProfileService
from workbench_agent.settings import ModelProfile
from workbench_agent.domain.admin_jobs import AdminAuth, AdminJob
from workbench_agent.domain.profiles import ModelProfileVersion
from workbench_agent.jobs.admin import AdminJobService
from workbench_agent.persistence.backends.model_profiles import list_team, list_visible
from workbench_agent.application.authorization import Authorization


class ModelProfileService:
    def __init__(self, backend):
        self.backend = backend
        self.profiles = ProfileService(backend)
        self.store = backend.configuration

    async def _get(self, uow, auth, agent, name):
        await self._authorize(uow, auth, agent)
        row = await self.store.one(uow, 'model_profiles', team_id=auth.team_id, name=name)
        if not row:
            raise not_found()
        owner = row['agent_id']
        if (agent is None and owner is not None) or (
            agent is not None and owner not in (None, agent)
        ):
            raise not_found()
        return dict(row)

    async def _authorize(self, uow, auth, agent):
        if agent is None:
            if auth.agent_scope is not None:
                raise DomainError('FORBIDDEN', 403)
            await Authorization(self.backend).current(uow, auth, minimum='admin')
        else:
            await self.profiles.agent(uow, auth, agent, 'admin')

    async def _authorized_agent(self, uow, auth, base_url, policy, secret_ref=None):
        if policy is None:
            raise DomainError('MODEL_PROFILE_UNAVAILABLE', 409)
        for binding in policy.bindings:
            if binding['team_id'] != auth.team_id or binding['base_url'] != base_url:
                continue
            if (
                secret_ref is not None
                and binding['secret_ref'] != secret_ref
                and not secret_ref.startswith('secret://managed-model/')
            ):
                continue
            for agent_id in binding['agent_ids']:
                if await self.store.one(
                    uow, 'agents', team_id=auth.team_id, agent_id=agent_id, status='active'
                ):
                    return agent_id
        raise DomainError('MODEL_PROFILE_UNAVAILABLE', 409)

    @staticmethod
    def _view(row):
        return {
            'profile_id': row['profile_id'],
            'name': row['name'],
            'revision': row['revision'],
            'active_version': row['active_version'],
            'draft': json.loads(row['draft']),
        }

    async def _view_with_ref(self, uow, row):
        view = self._view(row)
        version = None
        if row['active_version'] is not None:
            version = await self.store.one(
                uow,
                'model_profile_versions',
                team_id=row['team_id'],
                profile_id=row['profile_id'],
                version=row['active_version'],
            )
        view['active_ref'] = (
            {
                'resource_id': version['model_version_id'],
                'version': str(version['version']),
                'content_hash': version['hash'],
            }
            if version
            else None
        )
        return view

    async def get(self, auth, agent, name):
        async with self.backend.create_uow().begin_read() as uow:
            return await self._view_with_ref(uow, await self._get(uow, auth, agent, name))

    async def list(self, auth, agent, limit=20, after=None):
        async with self.backend.create_uow().begin_read() as uow:
            await self._authorize(uow, auth, agent)
            if agent is None:
                rows = await list_team(uow, auth.team_id, limit, after)
            else:
                rows = await list_visible(uow, auth.team_id, agent, limit, after)
            items = [await self._view_with_ref(uow, row) for row in rows[:limit]]
            return {
                'items': items,
                'next_cursor': items[-1]['profile_id'] if len(rows) > limit else None,
            }

    async def create(self, auth, agent, model, key):
        async with self.backend.create_uow().begin_write() as uow:
            await self._authorize(uow, auth, agent)

            async def operation():
                old = await self.store.one(
                    uow, 'model_profiles', team_id=auth.team_id, name=model.name
                )
                if old:
                    raise DomainError('MODEL_PROFILE_CONFLICT', 409)
                now = await self.backend.clock.now_ms(uow)
                row = dict(
                    team_id=auth.team_id,
                    agent_id=agent,
                    profile_id=str(uuid4()),
                    name=model.name,
                    scope='team' if agent is None else 'agent',
                    draft=canonical(model.model_dump(mode='json')),
                    active_version=None,
                    revision=0,
                    created_at=now,
                    updated_at=now,
                )
                await self.store.insert(uow, 'model_profiles', row)
                return self._view(row)

            return await self.profiles.idempotency.execute(
                uow,
                auth,
                agent,
                'model.create',
                key,
                model.model_dump(mode='json'),
                operation,
                status=201,
            )

    async def patch(self, auth, agent, name, changes, expected, key):
        async with self.backend.create_uow().begin_write() as uow:
            row = await self._get(uow, auth, agent, name)

            async def operation():
                self.profiles._check_revision(row, expected)
                if 'name' in changes and changes['name'] != name:
                    raise DomainError('INVALID_REQUEST', 422)
                try:
                    model = ModelProfile.model_validate({**json.loads(row['draft']), **changes})
                except ValidationError:
                    raise DomainError('INVALID_REQUEST', 422) from None
                now = await self.backend.clock.now_ms(uow)
                updated = dict(
                    row,
                    draft=canonical(model.model_dump(mode='json')),
                    revision=expected + 1,
                    updated_at=now,
                )
                changed = await self.store.change(
                    uow,
                    'model_profiles',
                    {
                        'team_id': auth.team_id,
                        'profile_id': row['profile_id'],
                        'revision': expected,
                    },
                    {'draft': updated['draft'], 'revision': expected + 1, 'updated_at': now},
                )
                if not changed:
                    raise DomainError('REVISION_CONFLICT', 409)
                return self._view(updated)

            return await self.profiles.idempotency.execute(
                uow,
                auth,
                agent,
                'model.patch',
                key,
                {'name': name, 'changes': changes, 'expected_revision': expected},
                operation,
            )

    async def set_credential(self, auth, agent, name, value, expected, secrets, policy):
        async with self.backend.create_uow().begin_write() as uow:
            row = await self._get(uow, auth, agent, name)
            self.profiles._check_revision(row, expected)
            if secrets is None:
                raise DomainError('MODEL_CREDENTIAL_STORE_UNAVAILABLE', 503)
            model = ModelProfile.model_validate_json(row['draft'])
            credential_agent = agent or await self._authorized_agent(
                uow, auth, model.base_url, policy
            )
            policy.require_managed(model.base_url, auth.team_id, credential_agent)
            ref = secrets.put(
                value,
                team_id=auth.team_id,
                agent_id=agent if row['agent_id'] else '*',
                profile_id=row['profile_id'],
                base_url=model.base_url,
            )
            data = model.model_copy(update={'secret_ref': ref}).model_dump(mode='json')
            now = await self.backend.clock.now_ms(uow)
            updated = dict(row, draft=canonical(data), revision=expected + 1, updated_at=now)
            changed = await self.store.change(
                uow,
                'model_profiles',
                {'team_id': auth.team_id, 'profile_id': row['profile_id'], 'revision': expected},
                {'draft': updated['draft'], 'revision': expected + 1, 'updated_at': now},
            )
            if not changed:
                raise DomainError('REVISION_CONFLICT', 409)
            await self.store.insert(
                uow,
                'audit_logs',
                dict(
                    audit_id=str(uuid4()),
                    team_id=auth.team_id,
                    actor=auth.subject,
                    object=agent or auth.team_id,
                    operation='model.credential.update',
                    decision='allowed',
                    trace=canonical({'profile_id': row['profile_id']}),
                    created_at=now,
                    updated_at=now,
                ),
            )
            return await self._view_with_ref(uow, updated)

    async def publish(self, auth, agent, name, expected, key):
        async with self.backend.create_uow().begin_write() as uow:
            row = await self._get(uow, auth, agent, name)

            async def operation():
                self.profiles._check_revision(row, expected)
                model = ModelProfile.model_validate_json(row['draft'])
                data = model.model_dump(mode='json')
                version = (row['active_version'] or 0) + 1
                model_id = str(uuid4())
                now = await self.backend.clock.now_ms(uow)
                await self.store.insert(
                    uow,
                    'model_profile_versions',
                    dict(
                        team_id=auth.team_id,
                        model_version_id=model_id,
                        profile_id=row['profile_id'],
                        version=version,
                        provider=model.provider,
                        adapter=model.adapter,
                        protocol=model.protocol,
                        base_url=model.base_url,
                        model_id=model.model_id,
                        secret_ref=model.secret_ref,
                        limits=canonical(data),
                        capabilities='{}',
                        hash=digest(data),
                        created_at=now,
                        updated_at=now,
                    ),
                )
                changed = await self.store.change(
                    uow,
                    'model_profiles',
                    {
                        'team_id': auth.team_id,
                        'profile_id': row['profile_id'],
                        'revision': expected,
                    },
                    {'active_version': version, 'revision': expected + 1, 'updated_at': now},
                )
                if not changed:
                    raise DomainError('REVISION_CONFLICT', 409)
                return ModelProfileVersion(
                    model_version_id=model_id,
                    profile_id=row['profile_id'],
                    name=model.name,
                    version=str(version),
                    content_hash=digest(data),
                    configuration_json=canonical(data),
                ).model_dump(mode='json')

            return await self.profiles.idempotency.execute(
                uow,
                auth,
                agent,
                'model.publish.draft',
                key,
                {'name': name, 'expected_revision': expected},
                operation,
                status=201,
            )

    async def probe(self, auth, agent, name, key, policy=None):
        async with self.backend.create_uow().begin_write() as uow:
            await self._authorize(uow, auth, agent)

            async def operation():
                row = await self._get(uow, auth, agent, name)
                if row['active_version'] is None:
                    raise DomainError('MODEL_PROFILE_UNAVAILABLE', 409)
                version = await self.store.one(
                    uow,
                    'model_profile_versions',
                    team_id=auth.team_id,
                    profile_id=row['profile_id'],
                    version=row['active_version'],
                )
                if not version:
                    raise DomainError('MODEL_PROFILE_UNAVAILABLE', 409)
                probe_agent = agent or await self._authorized_agent(
                    uow, auth, version['base_url'], policy, version['secret_ref']
                )
                payload = {
                    'model_version_id': version['model_version_id'],
                    'hash': version['hash'],
                    'version': version['version'],
                    'subject': auth.subject,
                    'identity_kind': auth.identity_kind,
                    'agent_scope': auth.agent_scope,
                }
                encoded = canonical(payload)
                authz = AdminAuth(
                    team_id=auth.team_id,
                    agent_id=probe_agent,
                    subject=auth.subject,
                    can_manage=True,
                )
                job = await self.backend.admin_jobs.submit(
                    uow, authz, 'model_probe', encoded, digest({'name': name}), key
                )
                from dataclasses import asdict

                return asdict(job)

            result = await self.profiles.idempotency.execute(
                uow, auth, agent, 'model.probe', key, {'name': name}, operation, status=202
            )
            return AdminJob(**result.body)
