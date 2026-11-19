"""Exercise recovery against the registered RCA graph and synthetic source policy."""
from integration.test_synthetic_alert_v2 import event, ingress, publish_v13, signed
from support.jobs import claim
from workbench_agent.application.runs import RunService
from workbench_agent.domain.runs import ResumeRun
from workbench_agent.jobs.worker import DeploymentConnectorPolicy, Execution, Worker
from workbench_agent.runtime.graphs.registry import foundation_worker_registry


class RcaRecovery:
    def __init__(self, platform, graph_id='rca.synthetic'):
        self.platform = platform
        self.graph_id = graph_id
        self.registry = foundation_worker_registry(platform.backend)
        self.policy = DeploymentConnectorPolicy(lambda: {'v2-test': {
            'subject': platform.auth.subject, 'team_id': platform.auth.team_id,
            'agent_ids': [platform.agent_id]}})
        self.registry.reconciler.connector_policy = self.policy
        self.worker = Worker(platform.backend, 'rca-recovery', self.registry, connector_policy=self.policy)
        self.executor = self.registry._executors[graph_id]
        self.graph = self.executor.registry.resolve(graph_id, self.executor.registry.state_schema_version(graph_id))

    async def admit(self, *, conflict=True):
        await publish_v13(self.platform)
        if self.graph_id != 'rca.synthetic':
            from integration.test_graph_catalog_api import publish_graph
            await publish_graph(self.platform, self.platform.auth, self.platform.agent_id, self.graph_id)
        source = event()
        run = await ingress(self.platform).accept(*signed(source))
        business = {key: source[key] for key in ('region', 'service', 'environment', 'anchor', 'window_seconds')}
        if conflict:
            business['link_region'] = 'PH'
        state = {'run_id': run.run_id, 'mode': 'rca', 'business_context': business,
                 'actor': self.platform.auth.subject}
        return run, state

    async def start(self, *, interrupt_after=None, conflict=True):
        run, state = await self.admit(conflict=conflict)
        owned = await claim(self.platform)
        with self.executor.guard_provider.bind(owned.guard):
            await self.graph.ainvoke(state, {'configurable': {'thread_id': run.run_id}},
                context=Execution(self.platform.backend, owned, self.policy),
                interrupt_after=interrupt_after, durability='sync')
        return run, owned

    async def waiting_run(self):
        run, owned = await self.start()
        async with self.platform.backend.create_uow().begin_write() as uow:
            await self.registry._projectors[self.graph_id].project(uow, owned, None)
        view = await self.platform.get_run(run.run_id)
        assert view.waiting
        return run, dict(view.waiting, expected_revision=view.revision)

    async def resume(self, run, waiting):
        request = ResumeRun.model_validate({'interrupt_id': waiting['interrupt_id'],
            'expected_revision': waiting['expected_revision'],
            'response': {'kind': 'user_input', 'values': {'region': 'id'}}})
        return await RunService(self.platform.backend).resume(self.platform.auth, run.run_id, request, 'answer')
