"""SQLite knowledge adapter; callers own the enclosing transaction."""
from sqlalchemy import func, insert, select, text, update

from workbench_agent.application.idempotency import canonical
from workbench_agent.persistence import schema


class SQLiteKnowledgeStore:
    async def entry(self, uow, team_id, entry_id):
        table = schema.kb_entries
        row = (await uow.execute(select(table).where(table.c.team_id == team_id,
                                                     table.c.entry_id == entry_id))).mappings().first()
        return dict(row) if row else None

    async def version(self, uow, team_id, entry_id, version):
        table = schema.kb_entry_versions
        row = (await uow.execute(select(table).where(table.c.team_id == team_id,
            table.c.entry_id == entry_id, table.c.version == version))).mappings().first()
        return dict(row) if row else None

    async def entries(self, uow, team_id, *, limit, after, filters):
        table = schema.kb_entries
        statement = select(table).where(table.c.team_id == team_id)
        if after:
            statement = statement.where(table.c.entry_id > after)
        if filters.get('status'):
            statement = statement.where(table.c.status == filters['status'])
        return [dict(row) for row in (await uow.execute(statement.order_by(table.c.entry_id).limit(limit))).mappings()]

    async def versions(self, uow, team_id, entry_id, *, limit, after):
        table = schema.kb_entry_versions
        statement = select(table).where(table.c.team_id == team_id, table.c.entry_id == entry_id)
        if after is not None:
            statement = statement.where(table.c.version < after)
        return [dict(row) for row in (await uow.execute(statement.order_by(table.c.version.desc()).limit(limit))).mappings()]

    async def insert_entry(self, uow, row):
        await uow.execute(insert(schema.kb_entries).values(**row))

    async def insert_version(self, uow, row):
        await uow.execute(insert(schema.kb_entry_versions).values(**row))

    async def cas_entry(self, uow, team_id, entry_id, expected_revision, changes):
        table = schema.kb_entries
        result = await uow.execute(update(table).where(table.c.team_id == team_id,
            table.c.entry_id == entry_id, table.c.revision == expected_revision).values(**changes))
        return result.rowcount == 1

    async def next_version(self, uow, team_id, entry_id):
        table = schema.kb_entry_versions
        result = await uow.execute(select(func.max(table.c.version)).where(
            table.c.team_id == team_id, table.c.entry_id == entry_id))
        return (result.scalar_one() or 0) + 1

    async def replace_chunks(self, uow, team_id, entry_id, version, draft, chunks):
        await uow.execute(text('DELETE FROM kb_fts WHERE team_id=:team AND entry_id=:entry'),
                          {'team': team_id, 'entry': entry_id})
        if version is None:
            return
        statement = text('''INSERT INTO kb_fts (title,content,tags,aliases,team_id,entry_id,version,
            chunk_id,heading_path,line_start,line_end,content_hash,chunker_version)
            VALUES (:title,:content,:tags,:aliases,:team,:entry,:version,:chunk_id,:heading_path,
                    :line_start,:line_end,:content_hash,:chunker_version)''')
        for chunk in chunks:
            await uow.execute(statement, {'title': draft.title, 'content': chunk.content,
                'tags': ' '.join(draft.tags), 'aliases': ' '.join(draft.aliases),
                'team': team_id, 'entry': entry_id, 'version': version, 'chunk_id': chunk.chunk_id,
                'heading_path': canonical(chunk.heading_path), 'line_start': chunk.line_start,
                'line_end': chunk.line_end, 'content_hash': chunk.content_hash,
                'chunker_version': chunk.chunker_version})

    async def search_current(self, uow, scope, query, *, limit):
        import re
        tokens = re.findall(r'[\u4e00-\u9fff]+|[A-Za-z0-9_./-]+', query)[:32]
        if not tokens:
            return []
        long_tokens = []
        for token in tokens:
            if len(token) >= 3:
                long_tokens.append(token)
                if re.fullmatch(r'[\u4e00-\u9fff]+', token) and len(token) > 4:
                    long_tokens.extend(token[i:i + 3] for i in range(min(len(token) - 2, 20)))
        params = {'team': scope.team_id, 'service': scope.service, 'environment': scope.environment,
                  'region': scope.region, 'limit': limit}
        if long_tokens:
            params['query'] = ' OR '.join('"' + token.replace('"', '""') + '"' for token in long_tokens[:32])
            predicate = 'kb_fts MATCH :query'
            ordering = 'bm25(kb_fts, 4.0, 1.0, 2.0, 3.0), e.entry_id, kb_fts.chunk_id'
        else:
            clauses = []
            for index, token in enumerate(tokens):
                param = f'term{index}'
                params[param] = '%' + token.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
                clauses.append('(' + ' OR '.join(f"kb_fts.{column} LIKE :{param} ESCAPE '\\'"
                    for column in ('title', 'content', 'tags', 'aliases')) + ')')
            predicate = '(' + ' OR '.join(clauses) + ')'
            ordering = 'e.entry_id, kb_fts.chunk_id'
        statement = text(f'''SELECT kb_fts.*, v.content_json, v.created_at FROM kb_fts
            JOIN kb_entries e ON e.team_id=kb_fts.team_id AND e.entry_id=kb_fts.entry_id
                AND e.status='published' AND e.published_version=kb_fts.version
            JOIN kb_entry_versions v ON v.team_id=e.team_id AND v.entry_id=e.entry_id
                AND v.version=e.published_version
            WHERE e.team_id=:team
              AND (json_extract(v.content_json,'$.service') IS NULL OR json_extract(v.content_json,'$.service')=:service)
              AND (json_extract(v.content_json,'$.environment') IS NULL OR json_extract(v.content_json,'$.environment')=:environment)
              AND (json_extract(v.content_json,'$.region') IS NULL OR json_extract(v.content_json,'$.region')=:region)
              AND {predicate} ORDER BY {ordering} LIMIT :limit''')
        return [dict(row) for row in (await uow.execute(statement, params)).mappings()]

    async def indexed_chunks(self, uow, team_id):
        rows = await uow.execute(text('''SELECT entry_id,version,chunk_id,content_hash,content,title,tags,aliases,
            heading_path,line_start,line_end,chunker_version FROM kb_fts WHERE team_id=:team'''),
                                 {'team': team_id})
        return [dict(row) for row in rows.mappings()]

    async def gap(self, uow, team_id, gap_id):
        table = schema.knowledge_gaps
        row = (await uow.execute(select(table).where(table.c.team_id == team_id, table.c.gap_id == gap_id))).mappings().first()
        return dict(row) if row else None

    async def gap_by_digest(self, uow, team_id, digest):
        table = schema.knowledge_gaps
        row = (await uow.execute(select(table).where(table.c.team_id == team_id, table.c.dedup_digest == digest))).mappings().first()
        return dict(row) if row else None

    async def gap_by_replay(self, uow, team_id, run_id):
        table = schema.knowledge_gaps
        row = (await uow.execute(select(table).where(table.c.team_id == team_id,
            table.c.replay_run_id == run_id))).mappings().first()
        return dict(row) if row else None

    async def replay_gap_id(self, uow, team_id, run_id):
        table = schema.audit_logs
        statement = select(table.c.object).where(table.c.team_id == team_id,
            table.c.operation == 'kb.gap.replay.link', func.json_extract(table.c.trace, '$.run_id') == run_id).limit(1)
        return (await uow.execute(statement)).scalar_one_or_none()

    async def gaps(self, uow, team_id, *, limit, after, status):
        table = schema.knowledge_gaps
        statement = select(table).where(table.c.team_id == team_id,
            table.c.problem_type.in_(('kb.empty', 'kb.unanswerable', 'kb.invalid_citation')))
        if after:
            statement = statement.where(table.c.gap_id > after)
        if status:
            statement = statement.where(table.c.review_status == status)
        return [dict(row) for row in (await uow.execute(statement.order_by(table.c.gap_id).limit(limit))).mappings()]

    async def insert_gap(self, uow, values):
        await uow.execute(insert(schema.knowledge_gaps).values(**values))

    async def cas_gap(self, uow, team_id, gap_id, expected_revision, changes):
        table = schema.knowledge_gaps
        result = await uow.execute(update(table).where(table.c.team_id == team_id,
            table.c.gap_id == gap_id, table.c.gap_revision == expected_revision).values(**changes))
        return result.rowcount == 1
