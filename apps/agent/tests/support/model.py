"""Deterministic model adapter for tests of persistence and recovery."""

from langchain_core.messages import AIMessage
from workbench_agent.runtime.models.invoker import ModelInvoker


def install_test_model(backend):
    class Model:
        def build(self, *args, **kwargs):
            return self

        async def ainvoke(self, messages, **kwargs):
            return AIMessage(
                content='Test answer',
                usage_metadata={'input_tokens': 8, 'output_tokens': 3, 'total_tokens': 11},
            )

    backend._code_factory = lambda execution: ModelInvoker(
        backend, Model(), object(), execution=execution
    )
