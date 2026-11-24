"""Explicit, team-scoped inspection and rebuild of derived search data."""

from hashlib import sha256
from collections import Counter

from workbench_agent.application.errors import DomainError, not_found
from workbench_agent.knowledge.chunking import chunk_markdown
from workbench_agent.knowledge.models import KBDraft
from workbench_agent.application.idempotency import canonical


class KnowledgeMaintenance:
    def __init__(self, backend):
        self.backend, self.store = backend, backend.knowledge

    async def check(self, team_id):
        async with self.backend.create_uow().begin_read() as uow:
            rows, cursor = [], None
            while True:
                batch = await self.store.entries(uow, team_id, limit=100, after=cursor, filters={})
                rows.extend(batch)
                if len(batch) < 100:
                    break
                cursor = batch[-1]['entry_id']
            indexed = await self.store.indexed_chunks(uow, team_id)
            grouped = {}
            for row in indexed:
                grouped.setdefault(row['entry_id'], []).append(row)
            reports = {}
            for row in rows:
                entry_id = row['entry_id']
                actual = grouped.pop(entry_id, [])
                expected = ()
                draft = None
                if row['status'] == 'published':
                    version = await self.store.version(
                        uow, team_id, entry_id, row['published_version']
                    )
                    if not version:
                        raise DomainError('KB_INDEX_UNAVAILABLE', 503)
                    draft = KBDraft.model_validate_json(version['content_json'])
                    expected = chunk_markdown(draft.body_md)
                expected_set = {(chunk.chunk_id, chunk.content_hash) for chunk in expected}
                actual_set = {(chunk['chunk_id'], chunk['content_hash']) for chunk in actual}
                expected_by_id = {chunk.chunk_id: chunk for chunk in expected}
                counts = Counter(chunk['chunk_id'] for chunk in actual)
                mismatches = []
                for indexed in actual:
                    source = expected_by_id.get(indexed['chunk_id'])
                    if (
                        source
                        and draft
                        and (
                            indexed['title'] != draft.title
                            or indexed['tags'] != ' '.join(draft.tags)
                            or indexed['aliases'] != ' '.join(draft.aliases)
                            or indexed['heading_path'] != canonical(source.heading_path)
                            or str(indexed['line_start']) != str(source.line_start)
                            or str(indexed['line_end']) != str(source.line_end)
                            or indexed['chunker_version'] != source.chunker_version
                        )
                    ):
                        mismatches.append(indexed['chunk_id'])
                report = {
                    'missing': sorted(expected_set - actual_set),
                    'unexpected': sorted(actual_set - expected_set),
                    'stale_version': sorted(
                        {
                            str(chunk['version'])
                            for chunk in actual
                            if str(chunk['version']) != str(row['published_version'])
                        }
                    ),
                    'hash_mismatch': sorted(
                        chunk['chunk_id']
                        for chunk in actual
                        if sha256(chunk['content'].encode('utf-8')).hexdigest()
                        != chunk['content_hash']
                    ),
                    'duplicate_chunks': sorted(
                        chunk_id for chunk_id, count in counts.items() if count > 1
                    ),
                    'metadata_mismatch': sorted(set(mismatches)),
                }
                if any(report.values()):
                    reports[entry_id] = report
            for entry_id, chunks in grouped.items():
                reports[entry_id] = {'orphan': sorted(chunk['chunk_id'] for chunk in chunks)}
            return {'team_id': team_id, 'entries': reports}

    async def rebuild(self, team_id, entry_id, expected_revision):
        async with self.backend.create_uow().begin_write() as uow:
            row = await self.store.entry(uow, team_id, entry_id)
            if not row:
                raise not_found()
            if row['revision'] != expected_revision:
                raise DomainError('REVISION_CONFLICT', 409)
            if row['status'] != 'published':
                await self.store.replace_chunks(uow, team_id, entry_id, None, None, ())
            else:
                version = await self.store.version(uow, team_id, entry_id, row['published_version'])
                if version is None:
                    raise DomainError('KB_INDEX_UNAVAILABLE', 503)
                draft = KBDraft.model_validate_json(version['content_json'])
                await self.store.replace_chunks(
                    uow,
                    team_id,
                    entry_id,
                    row['published_version'],
                    draft,
                    chunk_markdown(draft.body_md),
                )
            return {'entry_id': entry_id, 'revision': expected_revision, 'rebuilt': True}
