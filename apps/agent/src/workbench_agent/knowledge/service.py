"""Team-scoped knowledge lifecycle with atomic publication and receipts."""
import json
from uuid import uuid4
from pydantic import ValidationError

from workbench_agent.application.authorization import Authorization
from workbench_agent.application.errors import DomainError, not_found
from workbench_agent.application.idempotency import Idempotency, canonical
from workbench_agent.domain.identity import ROLE_LEVEL
from workbench_agent.knowledge.chunking import chunk_markdown, normalize_markdown
from workbench_agent.knowledge.models import GapDraftTemplate, KBDraft, draft_hash


class KnowledgeService:
    def __init__(self, backend):
        self.backend, self.store = backend, backend.knowledge
        self.auth, self.idempotency = Authorization(backend), Idempotency(backend)

    async def authorize(self, uow, auth, minimum='viewer'):
        if auth.agent_scope is not None or auth.identity_kind == 'connector':
            raise not_found()
        return await self.auth.current(uow, auth, minimum=minimum)

    async def _entry(self, uow, auth, entry_id):
        row = await self.store.entry(uow, auth.team_id, entry_id)
        if row is None:
            raise not_found()
        return row

    async def _audit(self, uow, auth, operation, entry_id, **details):
        now = await self.backend.clock.now_ms(uow)
        await self.backend.configuration.insert(uow, 'audit_logs', {
            'audit_id': str(uuid4()), 'team_id': auth.team_id, 'actor': auth.subject,
            'object': entry_id, 'operation': operation, 'decision': 'allowed',
            'trace': canonical(details), 'created_at': now, 'updated_at': now})

    async def _view(self, uow, auth, row, *, include_draft=False):
        published = await self.store.version(uow, auth.team_id, row['entry_id'], row['published_version']) if row['published_version'] else None
        if not include_draft and not published:
            raise not_found()
        draft = json.loads(row['draft_json'])['content']
        shown = draft if include_draft else json.loads(published['content_json'])
        view = {'entry_id': row['entry_id'], 'title': shown['title'], 'kind': shown['kind'],
            'status': row['status'], 'revision': row['revision'], 'published_version': row['published_version'],
            'has_unpublished_changes': bool(published and draft_hash(KBDraft.model_validate(draft)) != published['content_hash']),
            'owner': row['owner'], 'created_at': row['created_at'], 'updated_at': row['updated_at'],
            'service': shown.get('service'), 'environment': shown.get('environment'), 'region': shown.get('region'),
            'tags': shown.get('tags', []), 'aliases': shown.get('aliases', [])}
        if include_draft:
            view['draft'] = draft
        return view

    async def get(self, auth, entry_id):
        async with self.backend.create_uow().begin_read() as uow:
            current = await self.authorize(uow, auth)
            return await self._view(uow, auth, await self._entry(uow, auth, entry_id),
                                    include_draft=ROLE_LEVEL[current.roles[0]] >= ROLE_LEVEL['maintainer'])

    async def list(self, auth, *, limit=50, after=None, q=None, kind=None, status=None,
                   service=None, environment=None, region=None):
        async with self.backend.create_uow().begin_read() as uow:
            current = await self.authorize(uow, auth)
            can_edit = ROLE_LEVEL[current.roles[0]] >= ROLE_LEVEL['maintainer']
            items, cursor = [], after
            while len(items) <= limit:
                rows = await self.store.entries(uow, auth.team_id, limit=100, after=cursor,
                                                filters={'status': status})
                if not rows:
                    break
                for row in rows:
                    cursor = row['entry_id']
                    if not can_edit and not row['published_version']:
                        continue
                    item = await self._view(uow, auth, row, include_draft=can_edit)
                    content = item.get('draft') or json.loads((await self.store.version(
                        uow, auth.team_id, row['entry_id'], row['published_version']))['content_json'])
                    if kind and item['kind'] != kind:
                        continue
                    if q and q.casefold() not in ' '.join([item['title'], *content.get('tags', []),
                                                           *content.get('aliases', [])]).casefold():
                        continue
                    if any(value and content.get(name) != value for name, value in
                           [('service', service), ('environment', environment), ('region', region)]):
                        continue
                    item.pop('draft', None)
                    items.append(item)
                    if len(items) > limit:
                        break
                if len(rows) < 100:
                    break
            return {'items': items[:limit], 'next_cursor': items[limit - 1]['entry_id'] if len(items) > limit else None}

    async def create(self, auth, draft: KBDraft, *, key: str):
        async with self.backend.create_uow().begin_write() as uow:
            await self.authorize(uow, auth, 'maintainer')
            async def operation():
                now, entry_id = await self.backend.clock.now_ms(uow), str(uuid4())
                await self.store.insert_entry(uow, {'team_id': auth.team_id, 'entry_id': entry_id,
                    'status': 'draft', 'published_version': None,
                    'draft_json': canonical({'content': draft.model_dump(mode='json'), 'origin': None}),
                    'revision': 0, 'owner': auth.subject, 'created_at': now, 'updated_at': now})
                await self._audit(uow, auth, 'kb.create', entry_id, revision=0)
                return await self._view(uow, auth, await self._entry(uow, auth, entry_id), include_draft=True)
            return await self.idempotency.execute_team(uow, auth, 'kb.create', key,
                draft.model_dump(mode='json'), operation, status=201)

    async def create_gap_draft_in(self, uow, auth, template: GapDraftTemplate):
        # The empty body is a private placeholder. Normal save/publish validates KBDraft.
        content = KBDraft(title=template.title, kind='faq', body_md='placeholder',
            service=template.service, environment=template.environment, region=template.region).model_dump(mode='json')
        content['body_md'] = ''
        now, entry_id = await self.backend.clock.now_ms(uow), str(uuid4())
        await self.store.insert_entry(uow, {'team_id': auth.team_id, 'entry_id': entry_id, 'status': 'draft',
            'published_version': None, 'draft_json': canonical({'content': content,
                'origin': {'gap_id': template.gap_id, 'run_id': template.run_id}}),
            'revision': 0, 'owner': auth.subject, 'created_at': now, 'updated_at': now})
        await self._audit(uow, auth, 'kb.create_gap_draft', entry_id, gap_id=template.gap_id)
        return await self._view(uow, auth, await self._entry(uow, auth, entry_id), include_draft=True)

    async def save(self, auth, entry_id, draft, *, expected_revision, key, origin=None):
        payload = {'entry_id': entry_id, 'revision': expected_revision,
                   'content': draft.model_dump(mode='json'), 'origin': origin}
        async with self.backend.create_uow().begin_write() as uow:
            await self.authorize(uow, auth, 'maintainer')
            async def operation():
                row = await self._entry(uow, auth, entry_id)
                if row['revision'] != expected_revision:
                    raise DomainError('REVISION_CONFLICT', 409)
                now = await self.backend.clock.now_ms(uow)
                previous = json.loads(row['draft_json'])
                changes = {'draft_json': canonical({'content': payload['content'],
                    'origin': origin if origin is not None else previous.get('origin')}),
                    'revision': expected_revision + 1, 'updated_at': now}
                if not await self.store.cas_entry(uow, auth.team_id, entry_id, expected_revision, changes):
                    raise DomainError('REVISION_CONFLICT', 409)
                await self._audit(uow, auth, 'kb.save', entry_id, revision=expected_revision + 1)
                return await self._view(uow, auth, await self._entry(uow, auth, entry_id), include_draft=True)
            return await self.idempotency.execute_team(uow, auth, 'kb.save', key, payload, operation)

    async def _publish(self, uow, auth, row, expected_revision, draft, note=None):
        version = await self.store.next_version(uow, auth.team_id, row['entry_id'])
        now, content_hash = await self.backend.clock.now_ms(uow), draft_hash(draft)
        await self.store.insert_version(uow, {'team_id': auth.team_id, 'entry_id': row['entry_id'],
            'version': version, 'content_json': canonical(draft.model_dump(mode='json')),
            'content_hash': content_hash, 'author': auth.subject, 'change_note': note, 'created_at': now})
        previous = json.loads(row['draft_json'])
        changes = {'status': 'published', 'published_version': version, 'revision': expected_revision + 1,
                   'updated_at': now, 'draft_json': canonical({'content': draft.model_dump(mode='json'),
                                                               'origin': previous.get('origin')})}
        if not await self.store.cas_entry(uow, auth.team_id, row['entry_id'], expected_revision, changes):
            raise DomainError('REVISION_CONFLICT', 409)
        await self.store.replace_chunks(uow, auth.team_id, row['entry_id'], version, draft, chunk_markdown(draft.body_md))
        return {'entry_id': row['entry_id'], 'version': version, 'content_hash': content_hash, 'created_at': now}

    async def publish(self, auth, entry_id, *, expected_revision, key):
        async with self.backend.create_uow().begin_write() as uow:
            await self.authorize(uow, auth, 'maintainer')
            async def operation():
                row = await self._entry(uow, auth, entry_id)
                if row['revision'] != expected_revision:
                    raise DomainError('REVISION_CONFLICT', 409)
                try:
                    draft = KBDraft.model_validate(json.loads(row['draft_json'])['content'])
                except ValidationError:
                    raise DomainError('KB_INVALID_CONTENT', 422) from None
                if row['published_version'] and row['status'] == 'published':
                    old = await self.store.version(uow, auth.team_id, entry_id, row['published_version'])
                    if old['content_hash'] == draft_hash(draft):
                        raise DomainError('KB_NO_DRAFT_CHANGES', 409)
                result = await self._publish(uow, auth, row, expected_revision, draft)
                await self._audit(uow, auth, 'kb.publish', entry_id, version=result['version'])
                return result
            return await self.idempotency.execute_team(uow, auth, 'kb.publish', key,
                {'entry_id': entry_id, 'revision': expected_revision}, operation, status=201)

    async def archive(self, auth, entry_id, *, expected_revision, key):
        async with self.backend.create_uow().begin_write() as uow:
            await self.authorize(uow, auth, 'maintainer')
            async def operation():
                row = await self._entry(uow, auth, entry_id)
                if row['revision'] != expected_revision:
                    raise DomainError('REVISION_CONFLICT', 409)
                if not row['published_version'] or row['status'] == 'archived':
                    raise DomainError('KB_INVALID_STATUS', 409)
                now = await self.backend.clock.now_ms(uow)
                if not await self.store.cas_entry(uow, auth.team_id, entry_id, expected_revision,
                        {'status': 'archived', 'revision': expected_revision + 1, 'updated_at': now}):
                    raise DomainError('REVISION_CONFLICT', 409)
                await self.store.replace_chunks(uow, auth.team_id, entry_id, None, None, ())
                await self._audit(uow, auth, 'kb.archive', entry_id, version=row['published_version'])
                return await self._view(uow, auth, await self._entry(uow, auth, entry_id), include_draft=True)
            return await self.idempotency.execute_team(uow, auth, 'kb.archive', key,
                {'entry_id': entry_id, 'revision': expected_revision}, operation)

    async def rollback(self, auth, entry_id, version, *, expected_revision, key):
        async with self.backend.create_uow().begin_write() as uow:
            await self.authorize(uow, auth, 'maintainer')
            async def operation():
                row = await self._entry(uow, auth, entry_id)
                if row['revision'] != expected_revision:
                    raise DomainError('REVISION_CONFLICT', 409)
                old = await self.store.version(uow, auth.team_id, entry_id, version)
                if old is None:
                    raise not_found()
                if row['status'] == 'published' and row['published_version'] == version:
                    raise DomainError('KB_NO_DRAFT_CHANGES', 409)
                result = await self._publish(uow, auth, row, expected_revision,
                    KBDraft.model_validate_json(old['content_json']), note=f'republished version {version}')
                await self._audit(uow, auth, 'kb.rollback', entry_id, version=result['version'], source_version=version)
                return result
            return await self.idempotency.execute_team(uow, auth, 'kb.rollback', key,
                {'entry_id': entry_id, 'version': version, 'revision': expected_revision}, operation, status=201)

    async def versions(self, auth, entry_id, *, limit=50, after=None):
        async with self.backend.create_uow().begin_read() as uow:
            await self.authorize(uow, auth)
            await self._entry(uow, auth, entry_id)
            rows = await self.store.versions(uow, auth.team_id, entry_id, limit=limit + 1, after=after)
            items = [{k: row[k] for k in ('entry_id', 'version', 'content_hash', 'author', 'change_note', 'created_at')}
                     for row in rows[:limit]]
            return {'items': items, 'next_cursor': items[-1]['version'] if len(rows) > limit else None}

    async def version(self, auth, entry_id, version):
        async with self.backend.create_uow().begin_read() as uow:
            await self.authorize(uow, auth)
            entry = await self._entry(uow, auth, entry_id)
            row = await self.store.version(uow, auth.team_id, entry_id, version)
            if row is None:
                raise not_found()
            content = json.loads(row['content_json'])
            return {**content, 'entry_id': entry_id, 'version': version, 'content_hash': row['content_hash'],
                    'author': row['author'], 'created_at': row['created_at'], 'status': entry['status'],
                    'chunks': [chunk.model_dump(mode='json') for chunk in chunk_markdown(content['body_md'])]}

    async def import_text(self, auth, entry_id, raw, *, filename, expected_revision, key):
        from hashlib import sha256
        if not filename.lower().endswith(('.md', '.markdown', '.txt')):
            raise DomainError('KB_INVALID_CONTENT', 422)
        try:
            body = normalize_markdown(raw)
        except (ValueError, UnicodeError):
            raise DomainError('KB_INVALID_CONTENT', 422) from None
        origin = {'filename': filename, 'sha256': sha256(raw).hexdigest()}
        payload = {'entry_id': entry_id, 'revision': expected_revision, **origin}
        async with self.backend.create_uow().begin_write() as uow:
            await self.authorize(uow, auth, 'maintainer')
            async def operation():
                row = await self._entry(uow, auth, entry_id)
                if row['revision'] != expected_revision:
                    raise DomainError('REVISION_CONFLICT', 409)
                current = json.loads(row['draft_json'])['content']
                draft = KBDraft.model_validate({**current, 'body_md': body})
                now = await self.backend.clock.now_ms(uow)
                changes = {'draft_json': canonical({'content': draft.model_dump(mode='json'), 'origin': origin}),
                           'revision': expected_revision + 1, 'updated_at': now}
                if not await self.store.cas_entry(uow, auth.team_id, entry_id, expected_revision, changes):
                    raise DomainError('REVISION_CONFLICT', 409)
                await self._audit(uow, auth, 'kb.import', entry_id, revision=expected_revision + 1, source_hash=origin['sha256'])
                return await self._view(uow, auth, await self._entry(uow, auth, entry_id), include_draft=True)
            return await self.idempotency.execute_team(uow, auth, 'kb.import', key, payload, operation)
