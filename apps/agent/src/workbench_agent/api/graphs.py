"""Read-only, team-scoped descriptions of installed graphs."""
from dataclasses import asdict

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select

from workbench_agent.api.auth import authenticate
from workbench_agent.application.authorization import Authorization
from workbench_agent.application.errors import not_found
from workbench_agent.persistence import schema
from workbench_agent.runtime.graphs.catalog import get_graph, graph_structure, list_graphs

router = APIRouter(prefix='/v1/graphs')


async def _counts(backend, auth):
    async with backend.create_uow().begin_read() as uow:
        await Authorization(backend).current(uow, auth, minimum='viewer')
        agents, profiles = schema.agents, schema.runtime_profiles
        query = select(profiles.c.graph_id, func.count()).select_from(
            agents.join(profiles, (agents.c.active_profile_id == profiles.c.profile_id)
                & (agents.c.team_id == profiles.c.team_id) & (agents.c.agent_id == profiles.c.agent_id)))
        query = query.where(agents.c.team_id == auth.team_id, agents.c.status == 'active',
            profiles.c.status == 'published')
        if auth.agent_scope is not None:
            query = query.where(agents.c.agent_id.in_(auth.agent_scope))
        return dict((await uow.execute(query.group_by(profiles.c.graph_id))).all())


def _summary(spec, counts):
    return {'graph_id': spec.graph_id, 'title': spec.title, 'summary': spec.one_liner,
        'status': spec.status, 'capabilities': asdict(spec.capabilities), 'agents_using': counts.get(spec.graph_id, 0)}


@router.get('')
async def graph_list(request: Request, auth=Depends(authenticate)):
    counts = await _counts(request.app.state.backend, auth)
    return {'items': [_summary(spec, counts) for spec in list_graphs()]}


@router.get('/{graph_id}')
async def graph_detail(graph_id: str, request: Request, auth=Depends(authenticate)):
    counts = await _counts(request.app.state.backend, auth)
    spec = get_graph(graph_id)
    if spec is None:
        raise not_found()
    return {**_summary(spec, counts), 'when_to_use': spec.when_to_use,
        'when_not_to_use': spec.when_not_to_use, 'steps_text': spec.steps_text,
        'notes': spec.notes, **graph_structure(spec.graph_id)}
