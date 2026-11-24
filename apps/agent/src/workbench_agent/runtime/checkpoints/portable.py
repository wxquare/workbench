"""Locked LangGraph async saver; authorization is invocation-local and explicit."""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    CheckpointTuple,
    get_checkpoint_metadata,
    WRITES_IDX_MAP,
)
from workbench_agent.persistence.checkpoints.codec import CheckpointCodec
from workbench_agent.persistence.errors import StorageError
from workbench_agent.domain.guards import EXECUTING_RUN_STATUSES


class InvocationGuard:
    def __init__(self):
        self._scope = ContextVar('checkpoint_authority', default=None)

    @contextmanager
    def bind(self, guard, *, delete_auth=None):
        token = self._scope.set((guard, delete_auth))
        try:
            yield
        finally:
            self._scope.reset(token)

    def require(self, run_id=None, *, delete=False):
        scope = self._scope.get()
        if (
            scope is None
            or (run_id is not None and scope[0].run_id != run_id)
            or (delete and not scope[1])
        ):
            raise PermissionError('checkpoint invocation authority required')
        return scope[0]

    def deletion_auth(self):
        scope = self._scope.get()
        if scope is None or scope[1] is None:
            raise PermissionError('checkpoint deletion identity required')
        return scope[1]


class PortableCheckpointSaver(BaseCheckpointSaver):
    def __init__(self, backend, guard_provider, codec=None):
        codec = codec or CheckpointCodec()
        super().__init__(serde=codec)
        self.backend, self.guard_provider, self.codec = backend, guard_provider, codec
        self.store = backend.checkpoints

    @staticmethod
    def config(run_id, namespace, checkpoint_id):
        return {
            'configurable': {
                'thread_id': run_id,
                'checkpoint_ns': namespace,
                'checkpoint_id': checkpoint_id,
            }
        }

    def _guard(self, config):
        return self.guard_provider.require(
            config['configurable']['thread_id'] if config is not None else None
        )

    async def aput(self, config, checkpoint, metadata, new_versions):
        guard = self._guard(config)
        cfg = config['configurable']
        namespace = cfg.get('checkpoint_ns', '')
        ptype, payload = self.codec.dumps_typed(checkpoint)
        mtype, meta = self.codec.dumps_typed(get_checkpoint_metadata(config, metadata))
        async with self.backend.create_uow().begin_write() as uow:
            await self.store.put_with_guard(
                uow,
                guard,
                dict(
                    namespace=namespace,
                    checkpoint_id=checkpoint['id'],
                    parent_id=cfg.get('checkpoint_id'),
                    payload_type=ptype,
                    payload=payload,
                    metadata_type=mtype,
                    metadata=meta,
                    codec_version=self.codec.version,
                    created_at=await self.backend.clock.now_ms(uow),
                ),
            )
        return self.config(guard.run_id, namespace, checkpoint['id'])

    async def aput_writes(self, config, writes, task_id, task_path=''):
        guard = self._guard(config)
        cfg = config['configurable']
        encoded = []
        for index, (channel, value) in enumerate(writes):
            ptype, payload = self.codec.dumps_typed(value)
            encoded.append(
                dict(
                    task_id=task_id,
                    write_index=WRITES_IDX_MAP.get(channel, index),
                    channel=channel,
                    payload_type=ptype,
                    payload=payload,
                    task_path=task_path,
                )
            )
        if sum(len(write['payload']) for write in encoded) > self.codec.max_bytes:
            raise ValueError('checkpoint writes exceed 2 MiB')
        try:
            async with asyncio.timeout(self.backend.operation_timeout):
                while True:
                    async with self.backend.create_uow().begin_write() as uow:
                        await self.backend.lease_guard.require(uow, guard, EXECUTING_RUN_STATUSES)
                        rows = await self.store.rows(
                            uow, guard, cfg.get('checkpoint_ns', ''), cfg['checkpoint_id']
                        )
                        if rows:
                            await self.store.writes_with_guard(
                                uow,
                                guard,
                                cfg.get('checkpoint_ns', ''),
                                cfg['checkpoint_id'],
                                encoded,
                            )
                            return
                    # Native async durability may schedule writes before aput. No
                    # transaction/connection remains open while waiting for it.
                    await asyncio.sleep(0.01)
        except TimeoutError:
            raise StorageError('STORAGE_BUSY') from None

    def decode(self, row, writes):
        if row['codec_version'] != self.codec.version:
            raise ValueError('unsupported checkpoint codec version')
        return CheckpointTuple(
            config=self.config(row['run_id'], row['namespace'], row['checkpoint_id']),
            checkpoint=self.codec.loads_typed((row['payload_type'], row['payload'])),
            metadata=self.codec.loads_typed((row['metadata_type'], row['metadata'])),
            parent_config=self.config(row['run_id'], row['namespace'], row['parent_id'])
            if row['parent_id']
            else None,
            pending_writes=[
                (
                    w['task_id'],
                    w['channel'],
                    self.codec.loads_typed((w['payload_type'], w['payload'])),
                )
                for w in writes
            ],
        )

    async def aget_tuple(self, config):
        normalized = {'configurable': dict(config['configurable'])}
        normalized['configurable'].setdefault('checkpoint_ns', '')
        async for value in self.alist(normalized, limit=1):
            return value
        return None

    async def alist(self, config, *, filter=None, before=None, limit=None):
        guard = self._guard(config)
        cfg = config['configurable'] if config else {}
        if limit is not None and limit < 0:
            raise ValueError('negative checkpoint limit')
        # Materialize within one bounded read UoW; never yield holding a transaction.
        result = []
        async with self.backend.create_uow().begin_read() as uow:
            rows = await self.store.rows(
                uow,
                guard,
                cfg.get('checkpoint_ns'),
                cfg.get('checkpoint_id'),
                before['configurable'].get('checkpoint_id') if before else None,
            )
            for row in rows:
                if limit is not None and len(result) >= limit:
                    break
                metadata = self.codec.loads_typed((row['metadata_type'], row['metadata']))
                if filter and not all(metadata.get(k) == v for k, v in filter.items()):
                    continue
                result.append(self.decode(row, await self.store.pending(uow, row)))
        for item in result:
            yield item

    async def adelete_thread(self, thread_id):
        guard = self.guard_provider.require(thread_id, delete=True)
        async with self.backend.create_uow().begin_write() as uow:
            auth = self.guard_provider.deletion_auth()
            if auth.team_id != guard.team_id:
                raise PermissionError('checkpoint tenant mismatch')
            run = await self.backend.runs.one(
                uow, 'runs', team_id=guard.team_id, run_id=guard.run_id
            )
            if run is None:
                raise PermissionError('checkpoint Run unavailable')
            from workbench_agent.application.authorization import Authorization

            await Authorization(self.backend).current(
                uow, auth, run['agent_id'], minimum='maintainer'
            )
            await self.store.delete_terminal(uow, guard)
