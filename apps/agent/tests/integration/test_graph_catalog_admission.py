"""Catalog capabilities agree with real run admission for all installed graphs."""

from uuid import uuid4
import pytest

from integration.test_graph_catalog_api import publish_graph
from integration.test_sop import definition
from integration.test_synthetic_alert_v2 import event, ingress, publish_v13, signed
from workbench_agent.application.errors import DomainError
from workbench_agent.application.runs import RunService
from workbench_agent.application.workflows import WorkflowService
from workbench_agent.domain.runs import CreateRun
from workbench_agent.runtime.graphs.catalog import get_graph

SCOPE = {
    'service': 'checkout',
    'environment': 'test',
    'region': 'id',
    'start_at': '2026-11-14T01:00:00+00:00',
    'end_at': '2026-11-14T01:30:00+00:00',
}
MODES = ('auto', 'qa', 'rca', 'code', 'sop', 'inspection')
GRAPHS = (
    'sop.conditional',
    'rca.synthetic',
    'rca.proposal',
    'rca.batch_proposal',
    'knowledge.scoped_excerpt',
    'conversation.single_turn',
    'conversation.multi_turn',
    'knowledge.citation_review',
)


@pytest.mark.parametrize('graph_id', GRAPHS)
async def test_catalog_modes_and_entrypoints_match_admission(platform, graph_id):
    spec = get_graph(graph_id)
    is_rca = graph_id.startswith('rca.')
    if is_rca:
        await publish_v13(platform)
        if graph_id != 'rca.synthetic':
            await publish_graph(platform, platform.auth, platform.agent_id, graph_id)
        assert spec.capabilities.modes == ('rca',)
        assert spec.capabilities.entrypoints == ('synthetic_alert',)
        accepted = await ingress(platform).accept(*signed(event()))
        assert accepted.run_id
    else:
        await publish_graph(platform, platform.auth, platform.agent_id, graph_id)
        expected = ('sop', 'inspection') if graph_id == 'sop.conditional' else ('qa',)
        assert spec.capabilities.modes == expected
        assert spec.capabilities.entrypoints == (
            ('manual', 'schedule') if graph_id == 'sop.conditional' else ('manual',)
        )

    workflow = None
    if graph_id == 'sop.conditional':
        workflow = await WorkflowService(platform.backend).publish(
            platform.auth, platform.agent_id, definition(), key='admission-workflow'
        )
    for mode in MODES:
        payload = {'agent_id': platform.agent_id, 'mode': mode, 'input': {'text': 'test'}}
        if graph_id.startswith('knowledge.'):
            payload['business_context'] = {'query_scope': SCOPE}
        if workflow and mode in ('sop', 'inspection'):
            payload['workflow_version_id'] = workflow.workflow_version_id
        request = CreateRun.model_validate(payload)
        if not is_rca and mode in spec.capabilities.modes:
            accepted = await RunService(platform.backend).create(
                platform.auth, request, str(uuid4())
            )
            assert accepted.run_id
        else:
            with pytest.raises(DomainError):
                await RunService(platform.backend).create(platform.auth, request, str(uuid4()))
