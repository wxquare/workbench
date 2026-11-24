"""Scoped retrieval of current published knowledge."""

import json
import re

from workbench_agent.application.errors import DomainError
from workbench_agent.knowledge.models import KBChunk, KBScope, SearchHit
from workbench_agent.knowledge.service import KnowledgeService
from workbench_agent.persistence.errors import StorageError


class KnowledgeSearch:
    def __init__(self, backend):
        self.backend = backend

    async def search(self, scope: KBScope, query: str, *, limit=3) -> list[SearchHit]:
        query = ' '.join(query.split())
        if not query or len(query) > 4000 or not re.search(r'[\u4e00-\u9fffA-Za-z0-9]', query):
            raise DomainError('KB_INVALID_CONTENT', 422)
        try:
            async with self.backend.create_uow().begin_read() as uow:
                rows = await self.backend.knowledge.search_current(uow, scope, query, limit=limit)
        except StorageError:
            raise DomainError('KB_INDEX_UNAVAILABLE', 503) from None
        result = []
        for row in rows:
            content = json.loads(row['content_json'])
            chunk = KBChunk(
                chunk_id=row['chunk_id'],
                heading_path=tuple(json.loads(row['heading_path'])),
                line_start=int(row['line_start']),
                line_end=int(row['line_end']),
                content=row['content'],
                content_hash=row['content_hash'],
                chunker_version=row['chunker_version'],
            )
            result.append(
                SearchHit(
                    entry_id=row['entry_id'],
                    version=int(row['version']),
                    chunk=chunk,
                    title=content['title'],
                    kind=content['kind'],
                    updated_at=row['created_at'],
                    source_id=f'kb:{row["entry_id"]}@{row["version"]}#{row["chunk_id"]}',
                )
            )
        return result

    async def preview(self, auth, scope: KBScope, query: str, *, limit=3):
        async with self.backend.create_uow().begin_read() as uow:
            await KnowledgeService(self.backend).authorize(uow, auth, 'maintainer')
            if scope.team_id != auth.team_id:
                raise DomainError('FORBIDDEN', 403)
        hits = await self.search(scope, query, limit=limit)
        return {'matches': [hit.model_dump(mode='json') for hit in hits], 'warnings': []}
