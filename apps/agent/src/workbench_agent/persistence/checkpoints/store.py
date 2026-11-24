"""Portable SQL checkpoint storage. Writes fence in the caller's transaction."""

import hashlib
from dataclasses import dataclass
from sqlalchemy import select, insert, update, delete
from workbench_agent.persistence import schema as s
from workbench_agent.domain.guards import EXECUTING_RUN_STATUSES, TERMINAL_RUN_STATUSES


def checkpoint_namespace_key(namespace):
    return hashlib.sha256(namespace.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class CheckpointRef:
    run_id: str
    namespace: str
    checkpoint_id: str


class CheckpointStore:
    def __init__(self, backend):
        self.backend = backend

    async def check_namespace(self, uow, guard, namespace):
        key = checkpoint_namespace_key(namespace)
        values = (
            (
                await uow.execute(
                    select(s.graph_checkpoints.c.namespace)
                    .where(
                        s.graph_checkpoints.c.team_id == guard.team_id,
                        s.graph_checkpoints.c.run_id == guard.run_id,
                        s.graph_checkpoints.c.namespace_hash == key,
                    )
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        if any(value != namespace for value in values):
            raise ValueError('checkpoint namespace hash collision')
        return key

    async def put_with_guard(self, uow, guard, payload) -> CheckpointRef:
        await self.backend.lease_guard.require(uow, guard, EXECUTING_RUN_STATUSES)
        key = await self.check_namespace(uow, guard, payload['namespace'])
        scope = (
            s.graph_checkpoints.c.team_id == guard.team_id,
            s.graph_checkpoints.c.run_id == guard.run_id,
            s.graph_checkpoints.c.namespace_hash == key,
            s.graph_checkpoints.c.checkpoint_id == payload['checkpoint_id'],
        )
        exists = (
            await uow.execute(select(s.graph_checkpoints.c.checkpoint_id).where(*scope))
        ).first()
        values = dict(
            payload,
            team_id=guard.team_id,
            run_id=guard.run_id,
            namespace_hash=key,
            run_epoch=guard.run_epoch,
        )
        if exists:
            await uow.execute(update(s.graph_checkpoints).where(*scope).values(**values))
        else:
            await uow.execute(insert(s.graph_checkpoints).values(**values))
        return CheckpointRef(guard.run_id, payload['namespace'], payload['checkpoint_id'])

    async def writes_with_guard(self, uow, guard, namespace, checkpoint_id, writes):
        await self.backend.lease_guard.require(uow, guard, EXECUTING_RUN_STATUSES)
        key = await self.check_namespace(uow, guard, namespace)
        for write in writes:
            scope = (
                s.graph_pending_writes.c.team_id == guard.team_id,
                s.graph_pending_writes.c.run_id == guard.run_id,
                s.graph_pending_writes.c.namespace_hash == key,
                s.graph_pending_writes.c.checkpoint_id == checkpoint_id,
                s.graph_pending_writes.c.task_id == write['task_id'],
                s.graph_pending_writes.c.write_index == write['write_index'],
            )
            exists = (
                await uow.execute(select(s.graph_pending_writes.c.channel).where(*scope))
            ).first()
            if exists and write['write_index'] >= 0:
                continue
            values = dict(
                write,
                team_id=guard.team_id,
                run_id=guard.run_id,
                namespace_hash=key,
                checkpoint_id=checkpoint_id,
                run_epoch=guard.run_epoch,
            )
            if exists:
                await uow.execute(update(s.graph_pending_writes).where(*scope).values(**values))
            else:
                await uow.execute(insert(s.graph_pending_writes).values(**values))

    async def rows(self, uow, guard, namespace=None, checkpoint_id=None, before=None):
        statement = select(s.graph_checkpoints).where(
            s.graph_checkpoints.c.team_id == guard.team_id,
            s.graph_checkpoints.c.run_id == guard.run_id,
        )
        if namespace is not None:
            key = await self.check_namespace(uow, guard, namespace)
            statement = statement.where(s.graph_checkpoints.c.namespace_hash == key)
        if checkpoint_id is not None:
            statement = statement.where(s.graph_checkpoints.c.checkpoint_id == checkpoint_id)
        if before is not None:
            statement = statement.where(s.graph_checkpoints.c.checkpoint_id < before)
        return (
            (
                await uow.execute(
                    statement.order_by(
                        s.graph_checkpoints.c.checkpoint_id.desc(),
                        s.graph_checkpoints.c.namespace_hash,
                    )
                )
            )
            .mappings()
            .all()
        )

    async def pending(self, uow, row):
        return (
            (
                await uow.execute(
                    select(s.graph_pending_writes)
                    .where(
                        s.graph_pending_writes.c.team_id == row['team_id'],
                        s.graph_pending_writes.c.run_id == row['run_id'],
                        s.graph_pending_writes.c.namespace_hash == row['namespace_hash'],
                        s.graph_pending_writes.c.checkpoint_id == row['checkpoint_id'],
                    )
                    .order_by(
                        s.graph_pending_writes.c.task_path,
                        s.graph_pending_writes.c.task_id,
                        s.graph_pending_writes.c.write_index,
                    )
                )
            )
            .mappings()
            .all()
        )

    async def delete_terminal(self, uow, guard):
        run = (
            (
                await uow.execute(
                    select(s.runs).where(
                        s.runs.c.team_id == guard.team_id, s.runs.c.run_id == guard.run_id
                    )
                )
            )
            .mappings()
            .first()
        )
        if run is None:
            raise PermissionError('checkpoint Run unavailable')
        lease = (
            await uow.execute(
                select(s.execution_leases.c.thread_id).where(
                    s.execution_leases.c.team_id == guard.team_id,
                    s.execution_leases.c.owner_run_id == guard.run_id,
                )
            )
        ).first()
        if run['status'] not in TERMINAL_RUN_STATUSES or lease:
            raise PermissionError('active Run checkpoint deletion prohibited')
        await uow.execute(
            delete(s.graph_pending_writes).where(
                s.graph_pending_writes.c.team_id == guard.team_id,
                s.graph_pending_writes.c.run_id == guard.run_id,
            )
        )
        # Break self-FKs first; SQLite enforces RESTRICT even for a bulk deletion.
        await uow.execute(
            update(s.graph_checkpoints)
            .where(
                s.graph_checkpoints.c.team_id == guard.team_id,
                s.graph_checkpoints.c.run_id == guard.run_id,
            )
            .values(parent_id=None)
        )
        await uow.execute(
            delete(s.graph_checkpoints).where(
                s.graph_checkpoints.c.team_id == guard.team_id,
                s.graph_checkpoints.c.run_id == guard.run_id,
            )
        )
