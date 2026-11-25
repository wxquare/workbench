import pytest
from langchain_core.messages import AIMessage, HumanMessage


@pytest.mark.parametrize(
    'used, messages, expected',
    [
        (0, [], 'compose'),
        (0, [AIMessage(content='answer')], 'compose'),
        (
            0,
            [AIMessage(content='', tool_calls=[{'name': 'read', 'args': {}, 'id': 'one'}])],
            'tools',
        ),
        (
            4,
            [AIMessage(content='', tool_calls=[{'name': 'read', 'args': {}, 'id': 'one'}])],
            'compose',
        ),
    ],
)
def test_model_route_respects_tool_calls_and_budget(used, messages, expected):
    from workbench_agent.runtime.graphs.tool_conversation import route_after_model

    assert route_after_model({'tool_calls_used': used, 'messages': messages}) == expected


def test_history_selection_keeps_recent_complete_turns_within_byte_limit(monkeypatch):
    from workbench_agent.runtime.graphs import tool_conversation

    monkeypatch.setattr(tool_conversation, 'MAX_HISTORY_BYTES', 8)
    turns = [
        (HumanMessage(content='old'), AIMessage(content='old')),
        (HumanMessage(content='中'), AIMessage(content='ok')),
        (HumanMessage(content='x'), AIMessage(content='y')),
    ]
    assert tool_conversation._bounded_history(turns, 3) == turns[1:]
    assert tool_conversation._bounded_history(turns, 1) == turns[-1:]
