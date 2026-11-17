"""SQL configuration adapter. Published version rows have no update/delete operation."""
from sqlalchemy import select, insert, update, func
from workbench_agent.persistence import schema

RESOURCES = {name: getattr(schema, name) for name in (
    'teams', 'team_members', 'agents', 'runtime_profiles', 'model_profiles',
    'model_profile_versions', 'skills', 'skill_versions', 'skill_drafts', 'tool_bindings', 'tool_binding_drafts', 'repo_snapshots',
    'idempotency_records', 'team_idempotency_records', 'audit_logs', 'workflow_versions')}
IMMUTABLE = frozenset({'model_profile_versions', 'skill_versions', 'tool_bindings', 'repo_snapshots', 'audit_logs', 'workflow_versions'})

class SQLiteConfigurationStore:
    @staticmethod
    def _where(table, scope):
        return [table.c[key].in_(value) if isinstance(value, (tuple, list, frozenset)) else table.c[key] == value
            for key, value in scope.items()]

    async def one(self, uow, resource, **scope):
        table = RESOURCES[resource]
        return (await uow.execute(select(table).where(*self._where(table, scope)))).mappings().first()

    async def rows(self, uow, resource, *, limit=100, after=None, **scope):
        table = RESOURCES[resource]
        primary = list(table.primary_key.columns)[0]
        statement = select(table).where(*self._where(table, scope)).order_by(primary).limit(min(limit, 100))
        if after:
            statement = statement.where(primary > after)
        return list((await uow.execute(statement)).mappings())

    async def latest_workflow_version(self, uow, team_id, agent_id, name):
        table = RESOURCES['workflow_versions']
        statement = select(func.max(table.c.version)).where(table.c.team_id == team_id,
            table.c.agent_id == agent_id, table.c.name == name)
        return (await uow.execute(statement)).scalar_one() or 0

    async def insert(self, uow, resource, values):
        await uow.execute(insert(RESOURCES[resource]).values(**values))

    async def change(self, uow, resource, scope, values):
        if resource in IMMUTABLE:
            raise ValueError('published resources are immutable')
        table = RESOURCES[resource]
        conditions = self._where(table, scope)
        if resource == 'runtime_profiles':
            conditions.append(table.c.status == 'draft')
        result = await uow.execute(update(table).where(*conditions).values(**values))
        return result.rowcount == 1
