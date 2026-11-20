"""Agent publication and version binding use only caller-UoW repository ports."""
import json
from uuid import uuid4
from workbench_agent.domain.profiles import ProfileDraft, RuntimeProfile, ModelProfileVersion, VersionRef
from workbench_agent.application.authorization import Authorization
from workbench_agent.application.errors import DomainError, not_found
from workbench_agent.application.idempotency import Idempotency, canonical, digest

class ProfileService:
    def __init__(self, backend):
        self.backend, self.store = backend, backend.configuration
        self.authorization, self.idempotency = Authorization(backend), Idempotency(backend)

    async def agent(self, uow, auth, agent_id=None, minimum='viewer'):
        if agent_id is None:
            scope = {'team_id': auth.team_id, 'status': 'active'}
            if auth.agent_scope is not None:
                scope['agent_id'] = auth.agent_scope
            preferred = await self.store.one(uow, 'agents', name='default', **scope)
            if preferred is None:
                rows = await self.store.rows(uow, 'agents', limit=1, **scope)
                preferred = rows[0] if rows else None
            agent_id = preferred['agent_id'] if preferred else None
            if agent_id is None:
                raise not_found()
        await self.authorization.current(uow, auth, agent_id, minimum)
        return dict(await self.store.one(uow, 'agents', team_id=auth.team_id, agent_id=agent_id))

    async def get_agent(self, auth, agent_id=None):
        async with self.backend.create_uow().begin_read() as uow:
            agent = await self.agent(uow, auth, agent_id)
            draft = await self.store.one(uow, 'runtime_profiles', team_id=auth.team_id,
                agent_id=agent['agent_id'], status='draft')
            agent['draft'] = json.loads(draft['profile_json']) if draft else None
            return agent

    async def list_agents(self, auth, limit=20, after=None):
        async with self.backend.create_uow().begin_read() as uow:
            await self.authorization.current(uow, auth)
            scope = {'team_id': auth.team_id, 'status': 'active'}
            if auth.agent_scope is not None:
                scope['agent_id'] = auth.agent_scope
            rows = await self.store.rows(uow, 'agents', limit=limit, after=after, **scope)
            return [dict(row) for row in rows]

    @staticmethod
    def _check_revision(agent, expected):
        if agent['revision'] != expected:
            raise DomainError('REVISION_CONFLICT', 409)

    @staticmethod
    def _model_version_number(ref):
        if not ref.version.isdecimal() or len(ref.version) > 18 or int(ref.version) < 1:
            raise DomainError('INVALID_VERSION_REF', 422)
        return int(ref.version)

    async def validate_refs(self, uow, auth, agent_id, draft):
        from workbench_agent.runtime.graphs.ids import ALL_GRAPH_IDS, GraphId
        if draft.graph_id not in ALL_GRAPH_IDS:
            raise DomainError('GRAPH_UNAVAILABLE', 422)
        for ref in (draft.default_model, *(binding.ref for binding in draft.role_models)):
            row = await self.store.one(uow, 'model_profile_versions', team_id=auth.team_id,
                model_version_id=ref.resource_id, version=self._model_version_number(ref), hash=ref.content_hash)
            if not row:
                raise not_found()
            owner = await self.store.one(uow, 'model_profiles', team_id=auth.team_id, profile_id=row['profile_id'])
            if owner['agent_id'] is not None and owner['agent_id'] != agent_id:
                raise not_found()
        if len(draft.skills) > 16:
            raise DomainError('TOO_MANY_SKILLS', 422)
        skill_owners = set()
        for refs, resource, id_field in ((draft.skills, 'skill_versions', 'skill_version_id'),
            (draft.tools, 'tool_bindings', 'binding_id'), (draft.repositories, 'repo_snapshots', 'snapshot_id')):
            for ref in refs:
                row = await self.store.one(uow, resource, team_id=auth.team_id, **{id_field: ref.resource_id})
                if not row: raise not_found()
                if resource == 'repo_snapshots':
                    from workbench_agent.application.repository_identity import snapshot_identity_hash
                    try: expected_hash = snapshot_identity_hash(row)
                    except ValueError: raise not_found() from None
                    if row['status'] != 'ready': raise not_found()
                else: expected_hash = row['hash']
                if expected_hash != ref.content_hash: raise not_found()
                if resource == 'skill_versions':
                    owner = await self.store.one(uow, 'skills', team_id=auth.team_id, skill_id=row['skill_id'])
                    if not owner or row['status'] != 'published' or owner['status'] != 'active':
                        raise not_found()
                    if owner['skill_id'] in skill_owners:
                        raise DomainError('DUPLICATE_SKILL', 422)
                    skill_owners.add(owner['skill_id'])
                else:
                    owner = row
                if owner['agent_id'] is not None and owner['agent_id'] != agent_id:
                    raise not_found()
                version = str(row['commit_sha'] if resource == 'repo_snapshots' else row['version'])
                if version != ref.version:
                    raise not_found()
        if draft.graph_id == GraphId.CONVERSATION_TOOL_USE:
            if not draft.tools or len(draft.tools) > 4:
                raise DomainError('TOOL_BINDINGS_INVALID', 422)
            aliases = set()
            for ref in draft.tools:
                row = await self.store.one(
                    uow, 'tool_bindings', team_id=auth.team_id,
                    binding_id=ref.resource_id,
                )
                data = json.loads(row['schema'])
                if (
                    row['alias'] in aliases
                    or data.get('read_only') is not True
                    or data.get('tool_name') != 'knowledge_search'
                ):
                    raise DomainError('TOOL_BINDINGS_INVALID', 422)
                aliases.add(row['alias'])

    async def _advance(self, uow, agent, **values):
        revision = agent['revision'] + 1
        now = await self.backend.clock.now_ms(uow)
        changed = await self.store.change(uow, 'agents',
            {'team_id': agent['team_id'], 'agent_id': agent['agent_id'], 'revision': agent['revision']},
            dict(values, revision=revision, updated_at=now))
        if not changed:
            raise DomainError('REVISION_CONFLICT', 409)
        return revision

    async def save_draft(self, auth, agent_id, draft: ProfileDraft, expected_revision, *, key, receipt=False):
        async with self.backend.create_uow().begin_write() as uow:
            agent = await self.agent(uow, auth, agent_id, 'maintainer')
            async def operation():
                self._check_revision(agent, expected_revision)
                await self.validate_refs(uow, auth, agent_id, draft)
                existing = await self.store.one(uow, 'runtime_profiles', team_id=auth.team_id,
                    agent_id=agent_id, status='draft')
                now = await self.backend.clock.now_ms(uow)
                values = dict(updated_at=now, graph_id=draft.graph_id, profile_json=canonical(draft.model_dump(mode='json')),
                    hash=digest(draft.model_dump(mode='json')))
                if existing:
                    await self.store.change(uow, 'runtime_profiles', {'profile_id': existing['profile_id']}, values)
                else:
                    await self.store.insert(uow, 'runtime_profiles', dict(team_id=auth.team_id, profile_id=str(uuid4()),
                        agent_id=agent_id, version=0, status='draft', created_at=now, **values))
                revision = await self._advance(uow, agent)
                return {'agent_id': agent_id, 'revision': revision, 'draft': draft.model_dump(mode='json')}
            result = await self.idempotency.execute(uow, auth, agent_id, 'agent.draft', key,
                {'draft': draft.model_dump(mode='json'), 'expected_revision': expected_revision}, operation)
        return result if receipt else result.body

    async def publish(self, auth, agent_id, expected_revision, *, key, receipt=False) -> RuntimeProfile:
        async with self.backend.create_uow().begin_write() as uow:
            agent = await self.agent(uow, auth, agent_id, 'maintainer')
            async def operation():
                self._check_revision(agent, expected_revision)
                row = await self.store.one(uow, 'runtime_profiles', team_id=auth.team_id, agent_id=agent_id, status='draft')
                if not row:
                    raise DomainError('DRAFT_REQUIRED', 409)
                draft = ProfileDraft.model_validate_json(row['profile_json'])
                await self.validate_refs(uow, auth, agent_id, draft)
                previous = await self.store.one(uow, 'runtime_profiles', profile_id=agent['active_profile_id']) if agent['active_profile_id'] else None
                version = previous['version'] + 1 if previous else 1
                now = await self.backend.clock.now_ms(uow)
                profile = RuntimeProfile(**draft.model_dump(), profile_id=str(uuid4()), agent_id=agent_id,
                    team_id=auth.team_id, version=str(version), content_hash=row['hash'])
                await self.store.insert(uow, 'runtime_profiles', dict(team_id=auth.team_id, profile_id=profile.profile_id,
                    agent_id=agent_id, version=version, graph_id=draft.graph_id,
                    profile_json=canonical(draft.model_dump(mode='json')), hash=profile.content_hash, status='published',
                    created_at=now, updated_at=now,
                    model_versions=canonical([draft.default_model.model_dump(mode='json'),
                        *(binding.ref.model_dump(mode='json') for binding in draft.role_models)]),
                    skill_versions=canonical([ref.model_dump() for ref in draft.skills]),
                    tool_versions=canonical([ref.model_dump() for ref in draft.tools]),
                    repo_versions=canonical([ref.model_dump() for ref in draft.repositories])))
                await self._advance(uow, agent, active_profile_id=profile.profile_id)
                return profile.model_dump(mode='json')
            result = await self.idempotency.execute(uow, auth, agent_id, 'agent.publish', key,
                {'expected_revision': expected_revision}, operation, status=201)
        return result if receipt else RuntimeProfile.model_validate(result.body)

    async def published(self, uow, auth, agent_id):
        agent = await self.agent(uow, auth, agent_id)
        row = await self.store.one(uow, 'runtime_profiles', team_id=auth.team_id,
            profile_id=agent['active_profile_id'], status='published')
        if not row:
            raise DomainError('PROFILE_UNAVAILABLE', 409)
        return RuntimeProfile(**json.loads(row['profile_json']), profile_id=row['profile_id'],
            agent_id=agent_id, team_id=auth.team_id, version=str(row['version']), content_hash=row['hash'])

    async def model_version(self, uow, auth, ref):
        row = await self.store.one(uow, 'model_profile_versions', team_id=auth.team_id,
            model_version_id=ref.resource_id, version=self._model_version_number(ref), hash=ref.content_hash)
        if not row:
            raise not_found()
        model = await self.store.one(uow, 'model_profiles', team_id=auth.team_id, profile_id=row['profile_id'])
        return ModelProfileVersion(model_version_id=row['model_version_id'], profile_id=row['profile_id'],
            name=model['name'], version=str(row['version']), content_hash=row['hash'], configuration_json=row['limits'])

    async def get_active_model(self, auth, agent_id=None):
        async with self.backend.create_uow().begin_read() as uow:
            agent = await self.agent(uow, auth, agent_id)
            profile = await self.published(uow, auth, agent['agent_id'])
            return await self.model_version(uow, auth, profile.default_model)

    async def get_model_draft(self, auth, name):
        async with self.backend.create_uow().begin_read() as uow:
            await self.authorization.current(uow, auth, minimum='admin')
            row = await self.store.one(uow, 'model_profiles', team_id=auth.team_id, name=name)
            if not row:
                raise not_found()
            return dict(row)

    async def publish_model(self, auth, agent_id, model, expected_revision, *, key):
        """Internal version publisher; model HTTP management/probes are T11."""
        async with self.backend.create_uow().begin_write() as uow:
            await self.agent(uow, auth, agent_id, 'admin')
            async def operation():
                row = await self.store.one(uow, 'model_profiles', team_id=auth.team_id, name=model.name)
                if not row:
                    raise not_found()
                self._check_revision(row, expected_revision)
                if row['agent_id'] is not None and row['agent_id'] != agent_id:
                    raise not_found()
                version = (row['active_version'] or 0) + 1
                data = model.model_dump(mode='json')
                model_id = str(uuid4())
                now = await self.backend.clock.now_ms(uow)
                await self.store.insert(uow, 'model_profile_versions', dict(team_id=auth.team_id,
                    model_version_id=model_id, profile_id=row['profile_id'], version=version,
                    provider=model.provider, adapter=model.adapter, protocol=model.protocol, base_url=model.base_url,
                    model_id=model.model_id, secret_ref=model.secret_ref, limits=canonical(data), capabilities='{}', hash=digest(data),
                    created_at=now, updated_at=now))
                await self.store.change(uow, 'model_profiles', {'profile_id': row['profile_id'], 'revision': expected_revision},
                    {'active_version': version, 'revision': expected_revision + 1, 'draft': canonical(data), 'updated_at': now})
                return ModelProfileVersion(model_version_id=model_id, profile_id=row['profile_id'], name=model.name,
                    version=str(version), content_hash=digest(data), configuration_json=canonical(data)).model_dump(mode='json')
            result = await self.idempotency.execute(uow, auth, agent_id, 'model.publish', key,
                {'model': model.model_dump(mode='json'), 'expected_revision': expected_revision}, operation, status=201)
        return ModelProfileVersion.model_validate(result.body)

    async def create_agent(self, auth, name, *, key, receipt=False):
        async with self.backend.create_uow().begin_write() as uow:
            await self.authorization.current(uow, auth, minimum='maintainer')
            if auth.agent_scope is not None:
                raise DomainError('FORBIDDEN', 403)
            async def operation():
                if await self.store.one(uow, 'agents', team_id=auth.team_id, name=name):
                    raise DomainError('AGENT_NAME_CONFLICT', 409)
                default_model = None
                after = None
                while default_model is None:
                    models = await self.store.rows(uow, 'model_profiles', team_id=auth.team_id,
                        agent_id=None, scope='team', after=after)
                    for model in models:
                        if model['active_version'] is None:
                            continue
                        version = await self.store.one(uow, 'model_profile_versions', team_id=auth.team_id,
                            profile_id=model['profile_id'], version=model['active_version'])
                        if version is not None:
                            default_model = VersionRef(resource_id=version['model_version_id'],
                                version=str(version['version']), content_hash=version['hash'])
                            break
                    if default_model is not None or len(models) < 100:
                        break
                    after = models[-1]['profile_id']
                if default_model is None:
                    raise DomainError('MODEL_PROFILE_UNAVAILABLE', 409)
                agent_id = str(uuid4())
                now = await self.backend.clock.now_ms(uow)
                await self.store.insert(uow, 'agents', dict(team_id=auth.team_id, agent_id=agent_id, name=name, created_at=now, updated_at=now))
                draft = ProfileDraft(default_model=default_model, allowed_modes=('qa',))
                await self.store.insert(uow, 'runtime_profiles', dict(team_id=auth.team_id, profile_id=str(uuid4()),
                    agent_id=agent_id, version=0, graph_id=draft.graph_id,
                    profile_json=canonical(draft.model_dump(mode='json')), hash=digest(draft.model_dump(mode='json')), status='draft', created_at=now, updated_at=now))
                return {'agent_id': agent_id, 'name': name, 'revision': 0}
            result = await self.idempotency.execute_team(uow, auth, 'agent.create', key, {'name': name}, operation, status=201)
        return result if receipt else result.body
