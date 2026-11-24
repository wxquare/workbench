"""T07 real SQLite, LangGraph and public service lifecycle helpers."""

import asyncio
import json
from sqlalchemy import select, update, func
from workbench_agent.domain.runs import AcceptedRun, ResumeRun
from workbench_agent.application.runs import RunService
from workbench_agent.application.context import ContextResolver
from workbench_agent.runtime.graphs.registry import (
    foundation_worker_registry,
    GraphRegistry,
    InterruptExecutor,
    InterruptProgressProjector,
)
from workbench_agent.runtime.checkpoints.portable import InvocationGuard, PortableCheckpointSaver
from workbench_agent.persistence.checkpoints.codec import CheckpointCodec
from support.interrupt_graph import build_interrupt_graph
from workbench_agent.jobs.worker import Worker, Execution, ExecutorRegistry
from workbench_agent.jobs.deadlines import DeadlineService
from support.jobs import claim, row, expire_lease
from workbench_agent.persistence import schema as s


class InterruptPlatform:
    def __init__(self, p):
        self.p = p
        self.service = RunService(p.backend)
        registry = foundation_worker_registry(p.backend)
        provider = InvocationGuard()
        saver = PortableCheckpointSaver(p.backend, provider, CheckpointCodec())
        graphs = GraphRegistry()
        graphs.register('conversation.multi_turn', 'dod-state-v2', build_interrupt_graph(saver))
        from workbench_agent.runtime.results import ResultProjector

        registry._executors['conversation.multi_turn'] = InterruptExecutor(graphs, saver, provider)
        registry._projectors['conversation.multi_turn'] = ResultProjector(
            p.backend, InterruptProgressProjector(p.backend, saver)
        )
        self.worker = Worker(p.backend, 'interrupt-worker', registry, renew_interval=0.02)

    async def publish_v2(self):
        from workbench_agent.domain.profiles import ProfileDraft
        from uuid import uuid4

        async with self.p.backend.create_uow().begin_read() as uow:
            published = await self.p.profiles.published(uow, self.p.auth, self.p.agent_id)
        current = await self.p.profiles.get_agent(self.p.auth, self.p.agent_id)
        changed = await self.p.profiles.save_draft(
            self.p.auth,
            self.p.agent_id,
            ProfileDraft(default_model=published.default_model, graph_id='conversation.multi_turn'),
            expected_revision=current['revision'],
            key=str(uuid4()),
        )
        await self.p.profiles.publish(
            self.p.auth, self.p.agent_id, expected_revision=changed['revision'], key=str(uuid4())
        )

    async def waiting_run(self, approval=False):
        await self.publish_v2()
        run = await self.p.create_run()
        owned = await claim(self.p)
        provider = InvocationGuard()
        saver = PortableCheckpointSaver(self.p.backend, provider, CheckpointCodec())
        graph = build_interrupt_graph(saver)
        with provider.bind(owned.guard):
            await graph.ainvoke(
                {
                    'run_id': run.run_id,
                    'input_text': 'question',
                    'requires_input': not approval,
                    'requires_approval': approval,
                    'actor': self.p.auth.subject,
                    'args_hash': 'a' * 64,
                    'messages': [],
                },
                {'configurable': {'thread_id': run.run_id}},
                context=Execution(self.p.backend, owned),
                durability='sync',
            )
        projector = self.worker.registry._projectors['conversation.multi_turn']
        async with self.p.backend.create_uow().begin_write() as uow:
            await projector.project(uow, owned, None)
        view = await self.p.get_run(run.run_id)
        assert view.waiting
        return run, dict(view.waiting, expected_revision=view.revision)

    async def resume(self, run, w, key, **changes):
        response = (
            {
                'kind': 'approval',
                'approval_id': w['approval_id'],
                'decision': changes.pop('decision', 'approve'),
                'expected_args_hash': changes.pop('expected_args_hash', w['args_hash']),
            }
            if w['kind'] == 'approval'
            else {
                'kind': 'user_input',
                'values': changes.pop('values', {'region': 'ID'}),
                'text': None,
            }
        )
        body = {
            'interrupt_id': w['interrupt_id'],
            'expected_revision': w['expected_revision'],
            'response': response,
        }
        body.update(changes)
        return await self.service.resume(
            self.p.auth, run.run_id, ResumeRun.model_validate(body), key
        )

    @staticmethod
    def is_accepted(value):
        return isinstance(value, AcceptedRun)

    async def resume_request_count(self, interrupt_id):
        async with self.p.backend.create_uow().begin_read() as uow:
            return (
                await uow.execute(
                    select(func.count())
                    .select_from(s.resume_requests)
                    .where(s.resume_requests.c.interrupt_id == interrupt_id)
                )
            ).scalar_one()

    async def current_scope(self, run):
        async with self.p.backend.create_uow().begin_read() as uow:
            return await self.p.backend.runs.current_scope(uow, self.p.auth.team_id, run.run_id)

    async def change_scope(self, run, scope):
        async with self.p.backend.create_uow().begin_write() as uow:
            return await ContextResolver(self.p.backend).query_scope(
                uow, self.p.auth, run.run_id, scope
            )

    async def saved_value(self, run, key):
        async with self.p.backend.create_uow().begin_read() as uow:
            saved = await self.p.backend.runs.checkpoint(uow, self.p.auth.team_id, run.run_id)
        return saved['values'].get(key)

    async def apply_then_crash(self, run):
        owned = await claim(self.p)
        executor = self.worker.registry._executors['conversation.multi_turn']
        await executor.apply_resume(Execution(self.p.backend, owned), mark=False)
        await expire_lease(self.p, owned.guard)

    async def recover_applied(self, run):
        assert await self.worker.run_once() == 'projected'

    async def resume_status(self, run):
        value = await row(self.p, s.resume_requests, run_id=run.run_id)
        return value['status']

    async def expire(self, run):
        async with self.p.backend.create_uow().begin_write() as uow:
            await uow.execute(
                update(s.runs).where(s.runs.c.run_id == run.run_id).values(waiting_deadline_at=0)
            )
        await DeadlineService(self.p.backend).scan()

    async def running_cancel_race(self):
        run = await self.p.create_run()
        started = asyncio.Event()
        stopped = asyncio.Event()
        captured = []

        class Blocking:
            async def execute(inner, execution):
                captured.append(execution.guard)
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()

        graph_id = 'conversation.multi_turn'
        registry = ExecutorRegistry()
        registry.register(graph_id, Blocking(), self.worker.registry._projectors[graph_id])
        worker = Worker(self.p.backend, 'cancel-worker', registry, renew_interval=0.01)
        task = asyncio.create_task(worker.run_once())
        await asyncio.wait_for(started.wait(), 2)
        await self.service.cancel(self.p.auth, run.run_id, 'cancel')
        assert await asyncio.wait_for(task, 2) == 'cancelled'
        assert stopped.is_set()
        assert (await self.p.get_run(run.run_id)).status == 'cancelled'
        from workbench_agent.persistence.errors import StaleExecution
        import pytest

        with pytest.raises(StaleExecution):
            async with self.p.backend.create_uow().begin_write() as uow:
                await self.p.backend.execution_events.append_execution(
                    uow, captured[0], 'phase.changed', {'phase': 'late'}
                )
