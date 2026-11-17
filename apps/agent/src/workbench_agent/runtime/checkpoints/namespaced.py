"""Constrain a nested graph's checkpoints to one Run-local namespace."""
from langgraph.checkpoint.base import BaseCheckpointSaver


class NamespacedSaver(BaseCheckpointSaver):
    def __init__(self, delegate, namespace):
        if not namespace or '|' in namespace:
            raise ValueError('invalid expert namespace')
        super().__init__(serde=delegate.serde)
        self.delegate, self.namespace = delegate, namespace

    def _in(self, config):
        if config is None:
            raise ValueError('Run configuration required')
        result = dict(config)
        values = dict(config['configurable'])
        child = values.get('checkpoint_ns', '')
        values['checkpoint_ns'] = self.namespace + ('|' + child if child else '')
        result['configurable'] = values
        return result

    def _out(self, config):
        if config is None:
            return None
        result = dict(config)
        values = dict(config['configurable'])
        namespace = values.get('checkpoint_ns', '')
        if namespace == self.namespace:
            values['checkpoint_ns'] = ''
        elif namespace.startswith(self.namespace + '|'):
            values['checkpoint_ns'] = namespace[len(self.namespace) + 1:]
        else:
            raise ValueError('checkpoint escaped expert namespace')
        result['configurable'] = values
        return result

    def _tuple(self, value):
        if value is None:
            return None
        return value._replace(config=self._out(value.config), parent_config=self._out(value.parent_config))

    async def aget_tuple(self, config):
        return self._tuple(await self.delegate.aget_tuple(self._in(config)))

    async def alist(self, config, *, filter=None, before=None, limit=None):
        async for item in self.delegate.alist(self._in(config), filter=filter,
                before=self._in(before) if before else None, limit=limit):
            yield self._tuple(item)

    async def aput(self, config, checkpoint, metadata, new_versions):
        return self._out(await self.delegate.aput(self._in(config), checkpoint, metadata, new_versions))

    async def aput_writes(self, config, writes, task_id, task_path=''):
        return await self.delegate.aput_writes(self._in(config), writes, task_id, task_path)

    async def adelete_thread(self, thread_id):
        raise PermissionError('expert cannot delete Run checkpoints')
