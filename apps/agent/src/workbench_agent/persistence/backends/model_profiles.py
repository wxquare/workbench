"""Agent-visible model profile queries."""
from sqlalchemy import or_, select
from workbench_agent.persistence import schema


async def list_visible(uow, team_id, agent_id, limit, after=None):
    table = schema.model_profiles
    query = select(table).where(table.c.team_id == team_id,
        or_(table.c.agent_id.is_(None), table.c.agent_id == agent_id))
    if after:
        query = query.where(table.c.profile_id > after)
    return list((await uow.execute(query.order_by(table.c.profile_id).limit(limit + 1))).mappings())
