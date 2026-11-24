"""Explicit opt-in probes; never print prompts, credentials, or provider errors."""

import asyncio
import os
import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool
from langchain_deepseek import ChatDeepSeek

pytestmark = pytest.mark.live


@pytest.fixture
def model():
    key = os.environ.get('DEEPSEEK_API_KEY')
    if not key:
        pytest.skip('DEEPSEEK_API_KEY not configured')
    return ChatDeepSeek(
        model='deepseek-flash',
        base_url='https://api.deepseek.com',
        api_key=key,
        max_retries=0,
        timeout=30,
        max_tokens=128,
        extra_body={'thinking': {'type': 'disabled'}},
    )


@pytest.mark.asyncio
async def test_text_and_usage(model):
    response = await model.ainvoke('Reply with exactly OK.')
    assert (
        response.content
        and response.usage_metadata
        and response.response_metadata.get('model_name')
    )


@pytest.mark.asyncio
async def test_tool_call_roundtrip(model):
    @tool
    def local_sum(a: int, b: int) -> int:
        """Add two integers locally."""
        return a + b

    first = await model.bind_tools([local_sum], tool_choice='required').ainvoke(
        'Use the tool to add 2 and 3.'
    )
    assert first.tool_calls and first.tool_calls[0]['name'] == 'local_sum'
    call = first.tool_calls[0]
    answer = await model.ainvoke(
        [
            HumanMessage(content='Use the tool to add 2 and 3.'),
            first,
            ToolMessage(content=str(local_sum.invoke(call['args'])), tool_call_id=call['id']),
        ]
    )
    assert '5' in answer.content


@pytest.mark.asyncio
async def test_stream_and_cancel(model):
    stream = model.astream('Count from one to one hundred, slowly.')
    seen = False
    try:
        async for part in stream:
            if part.content:
                seen = True
                break
    finally:
        await stream.aclose()
    assert seen


@pytest.mark.asyncio
async def test_json(model):
    from pydantic import BaseModel, StrictStr
    from workbench_agent.runtime.models.json_output import validate_json_output

    class Answer(BaseModel):
        summary: StrictStr

    response = await model.ainvoke(
        'Return JSON like {"summary":"ok"}.', response_format={'type': 'json_object'}
    )
    assert validate_json_output(
        response.content, Answer, response.response_metadata.get('finish_reason')
    ).summary


@pytest.mark.asyncio
async def test_budgeted_invoker_live(platform):
    from workbench_agent.runtime.models.invoker import ModelInvoker
    from workbench_agent.runtime.models.factory import ModelFactory, ModelCredentialPolicy
    from workbench_agent.connectors.secrets import SecretProvider
    from support.jobs import claim

    run = await platform.create_run(summary_token_reserve=8000)
    guard = (await claim(platform)).guard
    result = await ModelInvoker(
        platform.backend,
        ModelFactory(
            ModelCredentialPolicy(
                [
                    {
                        'team_id': platform.auth.team_id,
                        'agent_ids': [platform.agent_id],
                        'base_url': 'https://api.deepseek.com',
                        'secret_ref': 'env://DEEPSEEK_API_KEY',
                    }
                ]
            )
        ),
        SecretProvider(),
    ).invoke(guard, 'summary', [HumanMessage(content='Reply with exactly OK.')])
    assert result.status == 'ok' and result.response_model and result.usage.input_tokens is not None
    ledger = await platform.budgets.snapshot(platform.auth, run.run_id)
    assert ledger.model_attempts == 1 and not ledger.usage_pending and ledger.cost_pending


@pytest.mark.asyncio
async def test_versioned_probe_live(platform):
    from workbench_agent.runtime.models.deepseek import ModelProbe
    from workbench_agent.runtime.models.factory import ModelFactory, ModelCredentialPolicy
    from workbench_agent.connectors.secrets import SecretProvider

    version = await platform.profiles.get_active_model(platform.auth)
    report = await ModelProbe(
        platform.backend,
        ModelFactory(
            ModelCredentialPolicy(
                [
                    {
                        'team_id': platform.auth.team_id,
                        'agent_ids': [platform.agent_id],
                        'base_url': 'https://api.deepseek.com',
                        'secret_ref': 'env://DEEPSEEK_API_KEY',
                    }
                ]
            )
        ),
        SecretProvider(),
    ).run(platform.auth, platform.agent_id, version.ref)
    assert report.requested_model == 'deepseek-flash' and report.response_model
    assert all(report.capabilities.values())


@pytest.mark.asyncio
async def test_budgeted_json_invoker_live(platform):
    from pydantic import BaseModel, StrictStr
    from workbench_agent.runtime.models.invoker import ModelInvoker
    from workbench_agent.runtime.models.factory import ModelFactory, ModelCredentialPolicy
    from workbench_agent.connectors.secrets import SecretProvider
    from support.jobs import claim

    class Answer(BaseModel):
        summary: StrictStr

    await platform.create_run(summary_token_reserve=8000)
    guard = (await claim(platform)).guard
    result = await ModelInvoker(
        platform.backend,
        ModelFactory(
            ModelCredentialPolicy(
                [
                    {
                        'team_id': platform.auth.team_id,
                        'agent_ids': [platform.agent_id],
                        'base_url': 'https://api.deepseek.com',
                        'secret_ref': 'env://DEEPSEEK_API_KEY',
                    }
                ]
            )
        ),
        SecretProvider(),
    ).invoke(
        guard, 'summary', [HumanMessage(content='Provide a short greeting.')], output_schema=Answer
    )
    assert result.status == 'ok' and result.validated_data.summary
