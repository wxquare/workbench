"""Real file-backed partial parallel graphs and reopen helpers."""

from typing import Annotated, TypedDict
from langchain_core.messages import HumanMessage
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.types import interrupt
from workbench_agent.runtime.checkpoints.portable import InvocationGuard, PortableCheckpointSaver
from workbench_agent.persistence.checkpoints.codec import CheckpointCodec
from workbench_agent.persistence.backends.sqlite import SQLiteBackend
from support.jobs import claim


class ParallelState(TypedDict):
    run_id: str
    messages: Annotated[list, add_messages]


def parallel_graph(saver, calls):
    async def left(state):
        calls['left'] += 1
        return {'messages': [HumanMessage('left', id='left-stable')]}

    async def right(state):
        calls['right'] += 1
        answer = interrupt({'question': 'continue?'})
        return {'messages': [HumanMessage(answer, id='right-stable')]}

    graph = StateGraph(ParallelState)
    graph.add_node('left', left)
    graph.add_node('right', right)
    graph.add_edge(START, 'left')
    graph.add_edge(START, 'right')
    graph.add_edge('left', END)
    graph.add_edge('right', END)
    inner = graph.compile(checkpointer=True)
    parent = StateGraph(ParallelState)
    parent.add_node('diagnosis', inner)
    parent.add_edge(START, 'diagnosis')
    parent.add_edge('diagnosis', END)
    return parent.compile(checkpointer=saver)


class CheckpointPlatform:
    def __init__(self, platform):
        self.platform = platform
        self.guard_provider = InvocationGuard()
        self.calls = {'left': 0, 'right': 0}

    @property
    def saver(self):
        return PortableCheckpointSaver(
            self.platform.backend, self.guard_provider, CheckpointCodec()
        )

    async def claim(self, run_id):
        owned = await claim(self.platform)
        assert owned.guard.run_id == run_id
        return owned.guard

    async def save_partial_graph(self, guard, namespace=''):
        config = {'configurable': {'thread_id': guard.run_id}}
        with self.guard_provider.bind(guard):
            await parallel_graph(self.saver, self.calls).ainvoke(
                {'run_id': guard.run_id, 'messages': []}, config
            )
            all_saved = [item async for item in self.saver.alist(None)]
            saved = [item for item in all_saved if item.config['configurable']['checkpoint_ns']]
            if not saved:
                raise AssertionError('real subgraph namespace missing')
        return saved[0].config

    async def reopen_database(self):
        await self.platform.backend.close()
        self.platform.backend = SQLiteBackend(
            self.platform.settings.storage.database_url.get_secret_value()
        )


def checkpoint_platform(platform):
    return CheckpointPlatform(platform)
