import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, messages_to_dict
from pydantic import BaseModel, StrictStr

from support.jobs import claim
from workbench_agent.knowledge.models import KBScope
from workbench_agent.runtime.models.invoker import ModelInvoker
from workbench_agent.runtime.models.request_bound import request_token_bound


class Output(BaseModel):
    summary: StrictStr


class Scripted:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.messages = []

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        self.messages.append(messages)
        value = self.replies.pop(0)
        return AIMessage(
            content=value,
            response_metadata={'model_name': 'fixture', 'finish_reason': 'stop'},
            usage_metadata={
                'input_tokens': 10,
                'output_tokens': 5,
                'total_tokens': 15,
            },
        )


class Factory:
    def __init__(self, model):
        self.model = model

    def build(self, *args, **kwargs):
        return self.model


@pytest.mark.asyncio
async def test_invalid_then_valid_json_repairs_once_and_counts_two_requests(platform):
    run = await platform.create_run(summary_token_reserve=8000)
    guard = (await claim(platform)).guard
    model = Scripted(['not-json', '{"summary":"ok"}'])
    result = await ModelInvoker(platform.backend, Factory(model), object()).invoke(
        guard,
        'summary',
        [HumanMessage(content='answer')],
        output_schema=Output,
    )

    assert result.status == 'ok'
    assert result.validated_data.summary == 'ok'
    assert model.calls == 2
    assert result.structured_output_repairs == 1
    assert (await platform.budgets.snapshot(platform.auth, run.run_id)).model_attempts == 2


@pytest.mark.asyncio
async def test_two_invalid_outputs_do_not_receive_a_third_repair(platform):
    await platform.create_run(summary_token_reserve=8000)
    guard = (await claim(platform)).guard
    model = Scripted(['not-json', '{"summary":7}'])
    result = await ModelInvoker(platform.backend, Factory(model), object()).invoke(
        guard,
        'summary',
        [HumanMessage(content='answer')],
        output_schema=Output,
    )

    assert result.status == 'invalid_output'
    assert result.error_code == 'MODEL_INVALID_OUTPUT'
    assert result.structured_output_repairs == 1
    assert model.calls == 2


@pytest.mark.asyncio
async def test_repair_prompt_includes_the_invalid_response_as_untrusted_history(platform):
    await platform.create_run(summary_token_reserve=8000)
    guard = (await claim(platform)).guard
    model = Scripted(['not-json', '{"summary":"ok"}'])
    await ModelInvoker(platform.backend, Factory(model), object()).invoke(
        guard,
        'summary',
        [HumanMessage(content='answer')],
        output_schema=Output,
    )

    assert len(model.messages[1]) == len(model.messages[0]) + 2
    assert 'The prior JSON was invalid' in model.messages[1][-1].content


@pytest.mark.asyncio
async def test_repair_that_exceeds_the_frozen_summary_hold_does_not_dispatch(
    platform,
):
    # The first request consumes the small explicit hold. A repair remains a
    # second real request and must fail when the frozen reservation cannot fit.
    message = HumanMessage(content='answer' * 100)
    schema = json.dumps(Output.model_json_schema(), ensure_ascii=False)
    prompt = [
        SystemMessage(
            content='Return one JSON object matching this schema. Example: use valid JSON '
            'strings, numbers and arrays. Schema: ' + schema
        ),
        message,
    ]
    first_bound = request_token_bound(
        envelope={'messages': messages_to_dict(prompt), 'tools': []},
        message_count=len(prompt),
        max_output_tokens=4096,
    )
    hold = first_bound + 1
    await platform.create_run(token_limit=hold, summary_token_reserve=hold)
    guard = (await claim(platform)).guard
    model = Scripted(['not-json', '{"summary":"ok"}'])
    with pytest.raises(Exception):
        await ModelInvoker(platform.backend, Factory(model), object()).invoke(
            guard,
            'summary',
            [message],
            output_schema=Output,
        )
    assert model.calls == 1


@pytest.mark.asyncio
async def test_scope_labels_do_not_turn_content_query_into_scope_listing(
    platform,
):
    scope = KBScope(
        team_id=platform.auth.team_id,
        service='checkout',
        environment='test',
        region='id',
    )
    _, params, _, _ = platform.backend.knowledge._compile_query(
        scope, 'checkout test id topic-not-present', 3
    )
    assert params['query'] == '"topic-not-present"'
    assert 'checkout' not in params['query']
    assert 'test' not in params['query']
    assert 'id' not in params['query']
