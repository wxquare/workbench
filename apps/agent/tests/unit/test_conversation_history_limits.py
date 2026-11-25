from types import SimpleNamespace


def test_history_candidate_must_fit_bytes_and_model_prompt_limit(monkeypatch):
    from workbench_agent.runtime.graphs import conversation_qa

    monkeypatch.setattr(conversation_qa, 'MAX_HISTORY_BYTES', 8)
    model = SimpleNamespace(max_output_tokens=1)
    candidate = ('中', 'ok')
    turns = [('x', 'y')]
    assert conversation_qa._history_fits(candidate, turns, 'q', 'system', None, model, 10000)
    assert not conversation_qa._history_fits(
        candidate, [('long', 'turn')], 'q', 'system', None, model, 10000
    )
    assert not conversation_qa._history_fits(candidate, turns, 'q', 'system', None, model, 1)
