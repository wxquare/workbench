"""Authorized, agent-scoped task inventories."""
from sqlalchemy import and_, func, or_, select

from workbench_agent.application.authorization import Authorization
from workbench_agent.application.errors import DomainError
from workbench_agent.domain.runs import timestamp
from workbench_agent.persistence import schema


class InventoryService:
    def __init__(self, backend):
        self.backend = backend
        self.authorization = Authorization(backend)

    async def runs(self, auth, agent_id, *, limit, after=None, status=None, query=None):
        r, m = schema.runs, schema.messages
        question = (select(func.json_extract(m.c.content_ref, '$.input.text'))
            .where(m.c.team_id == r.c.team_id, m.c.run_id == r.c.run_id, m.c.role == 'user')
            .order_by(m.c.created_at, m.c.message_id).limit(1).correlate(r).scalar_subquery())
        mode = (select(func.json_extract(m.c.content_ref, '$.mode'))
            .where(m.c.team_id == r.c.team_id, m.c.run_id == r.c.run_id, m.c.role == 'user')
            .order_by(m.c.created_at, m.c.message_id).limit(1).correlate(r).scalar_subquery())
        filters = [r.c.team_id == auth.team_id, r.c.agent_id == agent_id]
        if status:
            filters.append(r.c.status == status)
        if query:
            filters.append(or_(func.lower(question).contains(query.lower(), autoescape=True),
                func.lower(r.c.run_id).contains(query.lower(), autoescape=True)))
        async with self.backend.create_uow().begin_read() as uow:
            await self.authorization.current(uow, auth, agent_id)
            if after:
                cursor = (await uow.execute(select(r.c.run_id, r.c.created_at)
                    .where(*filters, r.c.run_id == after))).mappings().first()
                if cursor is None:
                    raise DomainError('INVALID_CURSOR', 422)
                filters.append(or_(r.c.created_at < cursor['created_at'],
                    and_(r.c.created_at == cursor['created_at'], r.c.run_id < cursor['run_id'])))
            rows = (await uow.execute(select(r.c.run_id, r.c.thread_id, r.c.agent_id,
                r.c.status, r.c.phase, r.c.created_at, question.label('title'), mode.label('mode'))
                .where(*filters).order_by(r.c.created_at.desc(), r.c.run_id.desc()).limit(limit + 1))).mappings().all()
            summaries = await self.backend.runs.usage_summaries(
                uow, auth.team_id, [row['run_id'] for row in rows[:limit]]
            )
        items = [{'run_id': row['run_id'], 'thread_id': row['thread_id'], 'agent_id': row['agent_id'],
            'status': row['status'], 'phase': row['phase'], 'title': (row['title'] or '')[:200],
            'mode': row['mode'], 'created_at': timestamp(row['created_at']),
            'usage_summary': summaries.get(row['run_id'], {})} for row in rows[:limit]]
        return {'items': items, 'next_cursor': items[-1]['run_id'] if len(rows) > limit else None}

    async def threads(self, auth, agent_id, *, limit, after=None):
        t, m, r = schema.threads, schema.messages, schema.runs
        title = (select(func.json_extract(m.c.content_ref, '$.input.text'))
            .where(m.c.team_id == t.c.team_id, m.c.thread_id == t.c.thread_id, m.c.role == 'user')
            .order_by(m.c.created_at, m.c.message_id).limit(1).correlate(t).scalar_subquery())
        last_status = (select(r.c.status).where(r.c.team_id == t.c.team_id, r.c.thread_id == t.c.thread_id)
            .order_by(r.c.created_at.desc(), r.c.run_id.desc()).limit(1).correlate(t).scalar_subquery())
        last_run = (select(r.c.run_id).where(r.c.team_id == t.c.team_id, r.c.thread_id == t.c.thread_id)
            .order_by(r.c.created_at.desc(), r.c.run_id.desc()).limit(1).correlate(t).scalar_subquery())
        filters = [t.c.team_id == auth.team_id, t.c.agent_id == agent_id]
        async with self.backend.create_uow().begin_read() as uow:
            await self.authorization.current(uow, auth, agent_id)
            if after:
                cursor = (await uow.execute(select(t.c.thread_id, t.c.updated_at)
                    .where(*filters, t.c.thread_id == after))).mappings().first()
                if cursor is None:
                    raise DomainError('INVALID_CURSOR', 422)
                filters.append(or_(t.c.updated_at < cursor['updated_at'],
                    and_(t.c.updated_at == cursor['updated_at'], t.c.thread_id < cursor['thread_id'])))
            rows = (await uow.execute(select(t.c.thread_id, t.c.agent_id, t.c.created_at, t.c.updated_at,
                title.label('title'), last_status.label('status'), last_run.label('last_run_id')).where(*filters)
                .order_by(t.c.updated_at.desc(), t.c.thread_id.desc()).limit(limit + 1))).mappings().all()
        items = [{'thread_id': row['thread_id'], 'agent_id': row['agent_id'],
            'created_at': timestamp(row['created_at']), 'updated_at': timestamp(row['updated_at']),
            'title': (row['title'] or '')[:200], 'status': row['status'], 'last_run_id': row['last_run_id']} for row in rows[:limit]]
        return {'items': items, 'next_cursor': items[-1]['thread_id'] if len(rows) > limit else None}
