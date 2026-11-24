from sqlalchemy import select, update
from workbench_agent.persistence import schema as s
from workbench_agent.domain.guards import WorkerCapabilities
from workbench_agent.jobs.claim import JobService

CAPS = WorkerCapabilities(
    graph_ids=('conversation.multi_turn',),
    projection_graph_ids=('conversation.multi_turn',),
    can_reconcile=True,
)


async def claim(platform, worker_id='worker'):
    # Fixture publication defaults are read from the admitted frozen context.
    async with platform.backend.create_uow().begin_read() as uow:
        versions = (await uow.execute(select(s.runs.c.versions).limit(1))).scalar_one()
    from workbench_agent.domain.context import RunContext

    graph = RunContext.model_validate_json(versions).graph_id
    return await JobService(platform.backend).claim(
        WorkerCapabilities(graph_ids=(graph,), projection_graph_ids=(graph,), can_reconcile=True),
        worker_id,
    )


async def expire_lease(platform, guard):
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(s.execution_leases)
            .where(s.execution_leases.c.thread_id == guard.thread_id)
            .values(expires_at=0)
        )
        await uow.execute(
            update(s.run_jobs).where(s.run_jobs.c.job_id == guard.job_id).values(lease_until=0)
        )


async def row(platform, table, **scope):
    async with platform.backend.create_uow().begin_read() as uow:
        return (
            (await uow.execute(select(table).where(*(table.c[k] == v for k, v in scope.items()))))
            .mappings()
            .first()
        )


class ReleasingProjector:
    def __init__(self, backend):
        self.backend = backend

    async def project(self, uow, claim, result):
        await self.backend.jobs.release(uow, claim.guard, status='succeeded')


def guard_takeover_process(url, guard, role, first, barrier, acquired, release, results):
    """Independent SQLite process; no inherited connections or production force API."""
    import asyncio
    from workbench_agent.persistence.backends.sqlite import SQLiteBackend
    from workbench_agent.persistence.errors import StaleExecution

    async def run():
        backend = SQLiteBackend(url)
        barrier.wait(timeout=15)
        if role != first:
            await asyncio.to_thread(acquired.wait, 10)
        try:
            async with backend.create_uow().begin_write() as uow:
                if role == 'guard':
                    await backend.execution_events.append_execution(
                        uow, guard, 'phase.changed', {'owner': 'old'}
                    )
                    if first == 'guard':
                        # Shorten only the test lease after guard linearizes, then let
                        # DB time expire while the write transaction excludes takeover.
                        now = await backend.clock.now_ms(uow)
                        await uow.execute(
                            update(s.execution_leases)
                            .where(s.execution_leases.c.thread_id == guard.thread_id)
                            .values(expires_at=now + 100)
                        )
                        await uow.execute(
                            update(s.run_jobs)
                            .where(s.run_jobs.c.job_id == guard.job_id)
                            .values(lease_until=now + 100)
                        )
                else:
                    candidates = await backend.jobs.candidates(uow)
                    owned, _ = await backend.jobs.claim(uow, CAPS, 'new-worker', candidates)
                    if owned is None:
                        raise AssertionError('takeover did not claim')
                    await backend.execution_events.append_execution(
                        uow, owned.guard, 'phase.changed', {'owner': 'new'}
                    )
                if role == first:
                    acquired.set()
                    await asyncio.to_thread(release.wait, 10)
            results.put((role, 'committed'))
        except StaleExecution:
            results.put((role, 'STALE_EXECUTION'))
        except BaseException as error:
            results.put((role, type(error).__name__))
        finally:
            await backend.close()

    asyncio.run(run())


async def guarded_write(platform, guard, surface):
    """Demonstration adapter only: future T06/07 stores must fence identically.

    Real columns/tables are used, but this does not implement their codecs,
    pending-write indexing or budget/result application rules.
    """
    from sqlalchemy import insert

    async with platform.backend.create_uow().begin_write() as uow:
        await platform.backend.lease_guard.require(uow, guard, {'running'})
        if surface == 'event':
            await platform.backend.execution_events.append_execution(
                uow, guard, 'phase.changed', {'phase': 'late'}
            )
        elif surface == 'budget':
            await uow.execute(
                update(s.runs).where(s.runs.c.run_id == guard.run_id).values(budgets='{}')
            )
        elif surface == 'result':
            await uow.execute(
                update(s.runs)
                .where(s.runs.c.run_id == guard.run_id)
                .values(outcome_ref='stale-result')
            )
        else:
            await uow.execute(
                insert(s.graph_checkpoints).values(
                    team_id=guard.team_id,
                    run_id=guard.run_id,
                    namespace_hash='0' * 64,
                    namespace='',
                    checkpoint_id='late',
                    payload_type='bytes',
                    payload=b'late',
                    metadata_type='bytes',
                    metadata=b'{}',
                    codec_version='1',
                    run_epoch=guard.run_epoch,
                    created_at=0,
                )
            )
            if surface == 'pending':
                await uow.execute(
                    insert(s.graph_pending_writes).values(
                        team_id=guard.team_id,
                        run_id=guard.run_id,
                        namespace_hash='0' * 64,
                        checkpoint_id='late',
                        task_id='task',
                        write_index=0,
                        channel='late',
                        payload_type='bytes',
                        payload=b'late',
                        task_path='',
                        run_epoch=guard.run_epoch,
                    )
                )
