"""Versioned inert JSON codec with an explicit published type vocabulary."""

import base64
import json
import math
from langchain_core.messages import (
    HumanMessage,
    AIMessage,
    SystemMessage,
    ToolMessage,
    RemoveMessage,
)
from langgraph.types import Interrupt, Send

_MESSAGES = {
    cls.__name__: cls
    for cls in (HumanMessage, AIMessage, SystemMessage, ToolMessage, RemoveMessage)
}


class CheckpointCodec:
    version = 'dod-checkpoint-v1'
    max_bytes = 2 * 1024 * 1024

    def _encode(self, value):
        if value is None or type(value) in (str, bool, int):
            return value
        if type(value) is float and math.isfinite(value):
            return value
        if type(value) is bytes:
            return {'$': 'bytes', 'v': base64.b64encode(value).decode('ascii')}
        if type(value) in (list, tuple, set, frozenset):
            return {'$': type(value).__name__, 'v': [self._encode(v) for v in value]}
        if type(value) is dict:
            if any(type(k) is not str for k in value):
                raise ValueError('checkpoint keys must be strings')
            return {'$': 'dict', 'v': {k: self._encode(v) for k, v in value.items()}}
        if type(value).__name__ in _MESSAGES and type(value) is _MESSAGES[type(value).__name__]:
            return {'$': type(value).__name__, 'v': self._encode(value.model_dump(mode='python'))}
        if type(value) is Interrupt:
            return {'$': 'Interrupt', 'v': self._encode({'id': value.id, 'value': value.value})}
        if type(value) is Send:
            return {'$': 'Send', 'v': self._encode({'node': value.node, 'arg': value.arg})}
        raise ValueError('unsupported checkpoint value')

    def _decode(self, value):
        if value is None or type(value) in (str, bool, int):
            return value
        if type(value) is float and math.isfinite(value):
            return value
        if type(value) is not dict or set(value) != {'$', 'v'}:
            raise ValueError('invalid checkpoint encoding')
        tag, payload = value['$'], value['v']
        if tag == 'bytes':
            return base64.b64decode(payload, validate=True)
        if tag == 'dict':
            return {k: self._decode(v) for k, v in payload.items()}
        if tag in ('list', 'tuple', 'set', 'frozenset'):
            decoded = [self._decode(v) for v in payload]
            return {'list': list, 'tuple': tuple, 'set': set, 'frozenset': frozenset}[tag](decoded)
        if tag in _MESSAGES:
            return _MESSAGES[tag](**self._decode(payload))
        if tag == 'Interrupt':
            return Interrupt(**self._decode(payload))
        if tag == 'Send':
            return Send(**self._decode(payload))
        raise ValueError('unsupported checkpoint tag')

    def dumps_typed(self, value):
        payload = json.dumps(
            self._encode(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
        if len(payload) > self.max_bytes:
            raise ValueError('checkpoint exceeds 2 MiB')
        return ('dod-json', payload)

    def loads_typed(self, data):
        tag, payload = data
        if tag != 'dod-json' or len(payload) > self.max_bytes:
            raise ValueError('unsupported checkpoint encoding')
        try:
            return self._decode(json.loads(payload))
        except (TypeError, KeyError, AttributeError) as error:
            raise ValueError('invalid checkpoint encoding') from error
