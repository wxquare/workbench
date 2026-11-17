"""Team-scoped editable Skill drafts and immutable published versions."""
from io import BytesIO
import re
from uuid import uuid4
import zipfile
import yaml
from sqlalchemy import select

from workbench_agent.application.authorization import Authorization
from workbench_agent.application.errors import DomainError, not_found
from workbench_agent.application.idempotency import Idempotency
from workbench_agent.persistence import schema
from workbench_agent.skill_management.manifest import PREFIX, read_manifest, read_resource, validate_skill_metadata, write_manifest
from workbench_agent.skill_management.validator import InvalidBundle, validate_bundle


def bundle_for(name, description, content):
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name) or len(name) > 128:
        raise DomainError('INVALID_SKILL_NAME', 422)
    if not description.strip() or not content.strip() or len(description) > 1000 or len(content.encode()) > 32000:
        raise DomainError('INVALID_SKILL_CONTENT', 422)
    document = '---\n' + yaml.safe_dump({'name': name, 'description': description}, allow_unicode=True, sort_keys=False) + '---\n' + content.strip() + '\n'
    output = BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr(name + '/SKILL.md', document)
    try:
        return validate_bundle(output.getvalue(), name)
    except InvalidBundle:
        raise DomainError('INVALID_SKILL_CONTENT', 422) from None


class SharedSkillLibrary:
    def __init__(self, backend, objects=None):
        self.backend, self.store, self.objects = backend, backend.configuration, objects
        self.auth = Authorization(backend)
        self.idempotency = Idempotency(backend)

    async def authorize(self, uow, auth, role='viewer'):
        if auth.agent_scope is not None or auth.identity_kind == 'connector':
            raise not_found()
        await self.auth.current(uow, auth, minimum=role)

    async def skill(self, uow, auth, skill_id):
        row = await self.store.one(uow, 'skills', team_id=auth.team_id, skill_id=skill_id, agent_id=None, status='active')
        if not row:
            raise not_found()
        return dict(row)

    async def detail(self, uow, auth, skill_id):
        skill = await self.skill(uow, auth, skill_id)
        draft = await self.store.one(uow, 'skill_drafts', team_id=auth.team_id, skill_id=skill_id)
        skill['draft'] = {'description': draft['description'], 'content': draft['content']} if draft else None
        versions = await self._versions(uow, auth, skill_id)
        skill['versions'] = versions
        skill['latest_version'] = versions[0] if versions else None
        return skill

    async def _versions(self, uow, auth, skill_id):
        table = schema.skill_versions
        rows = (await uow.execute(select(table).where(table.c.team_id == auth.team_id,
            table.c.skill_id == skill_id, table.c.status == 'published').order_by(table.c.version.desc()))).mappings().all()
        return [{'resource_id': row['skill_version_id'], 'version': str(row['version']),
                 'content_hash': row['hash'], 'created_at': row['created_at']} for row in rows]

    async def list(self, auth, limit=100, after=None, query=None):
        async with self.backend.create_uow().begin_read() as uow:
            await self.authorize(uow, auth)
            table = schema.skills
            statement = select(table.c.skill_id).where(table.c.team_id == auth.team_id,
                table.c.agent_id.is_(None), table.c.status == 'active')
            if after:
                statement = statement.where(table.c.skill_id > after)
            if query:
                from sqlalchemy import or_
                statement = statement.where(or_(table.c.name.contains(query, autoescape=True), table.c.description.contains(query, autoescape=True)))
            rows = (await uow.execute(statement.order_by(table.c.skill_id).limit(limit + 1))).scalars().all()
            more = len(rows) > limit
            rows = rows[:limit]
            items = []
            for skill_id in rows:
                detail = await self.detail(uow, auth, skill_id)
                detail.pop('draft')
                items.append(detail)
            return {'items': items, 'next_cursor': rows[-1] if more else None}

    async def get(self, auth, skill_id):
        async with self.backend.create_uow().begin_read() as uow:
            await self.authorize(uow, auth)
            return await self.detail(uow, auth, skill_id)

    async def create(self, auth, name, description, content, key):
        bundle_for(name, description, content)
        async with self.backend.create_uow().begin_write() as uow:
            await self.authorize(uow, auth, 'maintainer')
            async def operation():
                existing = await self.store.one(uow, 'skills', team_id=auth.team_id, agent_id=None, name=name)
                if existing:
                    raise DomainError('SKILL_NAME_CONFLICT', 409)
                skill_id = str(uuid4())
                now = await self.backend.clock.now_ms(uow)
                await self.store.insert(uow, 'skills', dict(team_id=auth.team_id, agent_id=None, skill_id=skill_id,
                    name=name, description=description, status='active', revision=0, created_at=now, updated_at=now))
                await self.store.insert(uow, 'skill_drafts', dict(team_id=auth.team_id, skill_id=skill_id, name=name,
                    description=description, content=content, created_at=now, updated_at=now))
                return await self.detail(uow, auth, skill_id)
            return await self.idempotency.execute_team(uow, auth, 'shared-skill.create', key,
                {'name': name, 'description': description, 'content': content}, operation, status=201)

    async def save(self, auth, skill_id, description, content, revision, key):
        async with self.backend.create_uow().begin_write() as uow:
            await self.authorize(uow, auth, 'maintainer')
            async def operation():
                skill = await self.skill(uow, auth, skill_id)
                if skill['revision'] != revision:
                    raise DomainError('REVISION_CONFLICT', 409)
                bundle_for(skill['name'], description, content)
                now = await self.backend.clock.now_ms(uow)
                draft = await self.store.one(uow, 'skill_drafts', team_id=auth.team_id, skill_id=skill_id)
                if not draft:
                    raise DomainError('SKILL_DRAFT_UNAVAILABLE', 409)
                await self.store.change(uow, 'skill_drafts', {'skill_id': skill_id, 'team_id': auth.team_id},
                    {'description': description, 'content': content, 'updated_at': now})
                if not await self.store.change(uow, 'skills', {'skill_id': skill_id, 'team_id': auth.team_id, 'revision': revision},
                        {'revision': revision + 1, 'updated_at': now}):
                    raise DomainError('REVISION_CONFLICT', 409)
                return await self.detail(uow, auth, skill_id)
            return await self.idempotency.execute_team(uow, auth, 'shared-skill.save', key,
                {'skill_id': skill_id, 'description': description, 'content': content, 'revision': revision}, operation)

    async def publish(self, auth, skill_id, revision, key):
        payload = {'skill_id': skill_id, 'revision': revision}
        async with self.backend.create_uow().begin_read() as uow:
            await self.authorize(uow, auth, 'admin')
            replay = await self.idempotency.lookup(uow, auth, None, 'shared-skill.publish', key, payload)
            if replay:
                return replay
            skill = await self.skill(uow, auth, skill_id)
            if skill['revision'] != revision:
                raise DomainError('REVISION_CONFLICT', 409)
            draft = await self.store.one(uow, 'skill_drafts', team_id=auth.team_id, skill_id=skill_id)
            if not draft:
                raise DomainError('SKILL_DRAFT_UNAVAILABLE', 409)
            draft = dict(draft)
        objects = self.objects() if callable(self.objects) else self.objects
        if objects is None:
            raise DomainError('OBJECT_STORAGE_UNAVAILABLE', 503)
        bundle = bundle_for(skill['name'], draft['description'], draft['content'])
        try:
            bundle_ref = write_manifest(objects, bundle)
        except (OSError, ValueError):
            raise DomainError('OBJECT_STORAGE_UNAVAILABLE', 503) from None

        async with self.backend.create_uow().begin_write() as uow:
            await self.authorize(uow, auth, 'admin')
            async def operation():
                skill = await self.skill(uow, auth, skill_id)
                if skill['revision'] != revision:
                    raise DomainError('REVISION_CONFLICT', 409)
                draft = await self.store.one(uow, 'skill_drafts', team_id=auth.team_id, skill_id=skill_id)
                if not draft:
                    raise DomainError('SKILL_DRAFT_UNAVAILABLE', 409)
                versions = await self._versions(uow, auth, skill_id)
                version = int(versions[0]['version']) + 1 if versions else 1
                now, version_id = await self.backend.clock.now_ms(uow), str(uuid4())
                await self.store.insert(uow, 'skill_versions', dict(team_id=auth.team_id, skill_id=skill_id,
                    skill_version_id=version_id, version=version, hash=bundle.bundle_hash, bundle_ref=bundle_ref,
                    status='published', created_at=now, updated_at=now))
                if not await self.store.change(uow, 'skills', {'skill_id': skill_id, 'team_id': auth.team_id, 'revision': revision},
                        {'revision': revision + 1, 'description': draft['description'], 'updated_at': now}):
                    raise DomainError('REVISION_CONFLICT', 409)
                return {'resource_id': version_id, 'version': str(version), 'content_hash': bundle.bundle_hash}
            return await self.idempotency.execute_team(uow, auth, 'shared-skill.publish', key,
                payload, operation, status=201)

    async def version(self, auth, skill_id, version):
        async with self.backend.create_uow().begin_read() as uow:
            await self.authorize(uow, auth)
            skill = await self.skill(uow, auth, skill_id)
            row = await self.store.one(uow, 'skill_versions', team_id=auth.team_id, skill_id=skill_id,
                version=version, status='published')
            if not row:
                raise not_found()
            objects = self.objects() if callable(self.objects) else self.objects
            if objects is None:
                raise DomainError('OBJECT_STORAGE_UNAVAILABLE', 503)
            try:
                if row['bundle_ref'].startswith(PREFIX):
                    manifest = read_manifest(objects, row['bundle_ref'], skill['name'], row['hash'])
                    raw = read_resource(objects, manifest, 'SKILL.md')
                    validate_skill_metadata(manifest, raw)
                else:
                    bundle = validate_bundle(objects.get(row['bundle_ref']), skill['name'], allow_binary=True)
                    if bundle.bundle_hash != row['hash']:
                        raise ValueError('hash mismatch')
                    raw = bundle.resources['SKILL.md']
                content = raw.decode('utf-8')
            except (OSError, ValueError, KeyError, UnicodeError):
                raise DomainError('SKILL_VERSION_UNAVAILABLE', 409) from None
            return {'resource_id': row['skill_version_id'], 'version': str(row['version']),
                    'content_hash': row['hash'], 'content': content}
