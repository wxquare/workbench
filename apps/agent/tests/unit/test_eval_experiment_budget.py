import pytest

from workbench_agent.evaluation.journal import Journal
from workbench_agent.evaluation.runner import _config_hash, run_experiment
from unit.test_eval_runner import config, dataset, manifest


class CostedAPI:
    def __init__(self, cost):
        self.cost = cost
        self.created = []

    async def create_run(self, body, *, key):
        self.created.append(key)
        return {'run_id': f'run-{len(self.created)}'}

    async def provenance(self, run_id):
        ref = {'resource_id': 'r', 'version': '1', 'content_hash': 'a' * 64}
        return {
            'graph_id': 'knowledge.citation_review',
            'agent_profile': ref,
            'default_model': ref,
            'role_models': [],
        }

    async def usage(self, run_id):
        return {
            'amount_micros': self.cost,
            'currency': 'USD',
            'cost_pending': self.cost is None,
            'usage_pending': self.cost is None,
            'known_costs': {} if self.cost is None else {'USD': self.cost},
        }

    async def get_run(self, run_id):
        return {'status': 'succeeded', 'outcome': {'evidence_refs': []}}


def budgeted_config(tmp_path):
    cfg = config(tmp_path)
    cfg['experiment_budget_micros'] = 10
    cfg['pricing'] = {
        'currency': 'USD',
        'pricing_version': 'ceiling-v1',
        'input_micros_per_million': 40000,
        'output_micros_per_million': 40000,
        'tool_call_micros': 0,
        'scope': 'all_run_models_and_tools',
    }
    return cfg


@pytest.mark.asyncio
@pytest.mark.parametrize('cost', [6, None])
async def test_experiment_stops_before_next_run_if_cost_or_reservation_exhausts_budget(
    tmp_path, cost
):
    data = dataset()
    cfg = budgeted_config(tmp_path)
    if cost is None:
        cfg['experiment_budget_micros'] = 5
    pinned = manifest(data, cfg)
    pinned['config_hash'] = _config_hash(cfg)
    api = CostedAPI(cost)
    result = await run_experiment(
        data,
        cfg,
        manifest=pinned,
        journal=Journal(tmp_path / 'experiment/journal.jsonl'),
        api=api,
        split='dev',
        repeats=1,
        deadline_seconds=1,
    )
    assert len(api.created) == 1
    assert result['complete'] is False


@pytest.mark.asyncio
async def test_pending_cost_is_refreshed_after_recovery_before_new_dispatch(tmp_path):
    data, cfg = dataset(), budgeted_config(tmp_path)
    cfg['experiment_budget_micros'] = 5
    pinned = manifest(data, cfg)
    api = CostedAPI(None)
    journal = Journal(tmp_path / 'experiment/journal.jsonl')
    first = await run_experiment(
        data,
        cfg,
        manifest=pinned,
        journal=journal,
        api=api,
        split='dev',
        repeats=1,
        deadline_seconds=1,
    )
    assert first['complete'] is False and len(api.created) == 1
    api.cost = 0
    second = await run_experiment(
        data,
        cfg,
        manifest=pinned,
        journal=journal,
        api=api,
        split='dev',
        repeats=1,
        deadline_seconds=1,
    )
    assert second['complete'] is True and len(api.created) == 4
    from workbench_agent.evaluation.collector import read_rows

    saved = read_rows(tmp_path / 'experiment/results.jsonl')
    assert saved[0]['usage']['amount_micros'] == 0


@pytest.mark.asyncio
async def test_budget_requires_explicit_tool_cost_ceiling(tmp_path):
    data, cfg = dataset(), budgeted_config(tmp_path)
    del cfg['pricing']['tool_call_micros']
    pinned = manifest(data, cfg)
    api = CostedAPI(0)
    with pytest.raises(ValueError, match='cost ceilings'):
        await run_experiment(
            data,
            cfg,
            manifest=pinned,
            journal=Journal(tmp_path / 'experiment/journal.jsonl'),
            api=api,
            split='dev',
            repeats=1,
            deadline_seconds=1,
        )
    assert api.created == []


@pytest.mark.asyncio
async def test_missing_pending_flag_never_releases_unknown_reservation(tmp_path):
    class IncompleteUsage(CostedAPI):
        async def usage(self, run_id):
            return {'amount_micros': 0, 'currency': 'USD', 'cost_pending': False}

    data, cfg = dataset(), budgeted_config(tmp_path)
    cfg['experiment_budget_micros'] = 5
    api = IncompleteUsage(0)
    result = await run_experiment(
        data,
        cfg,
        manifest=manifest(data, cfg),
        journal=Journal(tmp_path / 'experiment/journal.jsonl'),
        api=api,
        split='dev',
        repeats=1,
        deadline_seconds=1,
    )
    assert len(api.created) == 1
    assert result['complete'] is False


@pytest.mark.asyncio
async def test_inconsistent_known_costs_do_not_release_reservation(tmp_path):
    class InconsistentUsage(CostedAPI):
        async def usage(self, run_id):
            return {
                'amount_micros': 0,
                'currency': 'USD',
                'cost_pending': False,
                'usage_pending': False,
                'known_costs': {'USD': 6},
            }

    data, cfg = dataset(), budgeted_config(tmp_path)
    cfg['experiment_budget_micros'] = 5
    api = InconsistentUsage(0)
    result = await run_experiment(
        data,
        cfg,
        manifest=manifest(data, cfg),
        journal=Journal(tmp_path / 'experiment/journal.jsonl'),
        api=api,
        split='dev',
        repeats=1,
        deadline_seconds=1,
    )
    assert len(api.created) == 1
    assert result['complete'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'usage',
    [
        {
            'amount_micros': 100,
            'currency': 'EUR',
            'cost_pending': False,
            'usage_pending': False,
            'known_costs': {'EUR': 100},
        },
        {
            'amount_micros': None,
            'currency': None,
            'cost_pending': True,
            'usage_pending': False,
            'known_costs': {'USD': 1, 'EUR': 1},
        },
    ],
)
async def test_mixed_or_other_currency_blocks_new_runs_across_recovery(tmp_path, usage):
    class MixedCurrency(CostedAPI):
        async def usage(self, run_id):
            return usage

    data, cfg = dataset(), budgeted_config(tmp_path)
    cfg['experiment_budget_micros'] = 100
    api = MixedCurrency(None)
    journal = Journal(tmp_path / 'experiment/journal.jsonl')
    for _ in range(2):
        result = await run_experiment(
            data,
            cfg,
            manifest=manifest(data, cfg),
            journal=journal,
            api=api,
            split='dev',
            repeats=1,
            deadline_seconds=1,
        )
        assert result['complete'] is False
    assert len(api.created) == 1


@pytest.mark.asyncio
async def test_pending_but_inconsistent_amount_blocks_new_runs(tmp_path):
    class InconsistentPending(CostedAPI):
        async def usage(self, run_id):
            return {
                'amount_micros': 80,
                'currency': 'USD',
                'cost_pending': False,
                'usage_pending': True,
                'known_costs': {'USD': 1},
            }

    data, cfg = dataset(), budgeted_config(tmp_path)
    cfg['experiment_budget_micros'] = 100
    api = InconsistentPending(None)
    result = await run_experiment(
        data,
        cfg,
        manifest=manifest(data, cfg),
        journal=Journal(tmp_path / 'experiment/journal.jsonl'),
        api=api,
        split='dev',
        repeats=1,
        deadline_seconds=1,
    )
    assert len(api.created) == 1
    assert result['complete'] is False


@pytest.mark.asyncio
async def test_usage_transport_failure_keeps_reservation_and_blocks_next_run(tmp_path):
    import httpx

    class UsageUnavailable(CostedAPI):
        async def usage(self, run_id):
            raise httpx.ReadTimeout('usage unavailable')

    data, cfg = dataset(), budgeted_config(tmp_path)
    cfg['experiment_budget_micros'] = 5
    api = UsageUnavailable(None)
    result = await run_experiment(
        data,
        cfg,
        manifest=manifest(data, cfg),
        journal=Journal(tmp_path / 'experiment/journal.jsonl'),
        api=api,
        split='dev',
        repeats=1,
        deadline_seconds=1,
    )
    assert len(api.created) == 1
    assert result['complete'] is False
    assert result['rows'][0]['usage'] is None


@pytest.mark.asyncio
async def test_reconciled_cost_is_persisted_even_when_no_next_run_is_allowed(tmp_path):
    from workbench_agent.evaluation.collector import read_rows

    data, cfg = dataset(), budgeted_config(tmp_path)
    cfg['experiment_budget_micros'] = 5
    pinned = manifest(data, cfg)
    api = CostedAPI(None)
    journal = Journal(tmp_path / 'experiment/journal.jsonl')
    await run_experiment(
        data,
        cfg,
        manifest=pinned,
        journal=journal,
        api=api,
        split='dev',
        repeats=1,
        deadline_seconds=1,
    )
    api.cost = 5
    await run_experiment(
        data,
        cfg,
        manifest=pinned,
        journal=journal,
        api=api,
        split='dev',
        repeats=1,
        deadline_seconds=1,
    )
    assert len(api.created) == 1
    saved = read_rows(tmp_path / 'experiment/results.jsonl')
    assert saved[0]['usage']['amount_micros'] == 5
    third = await run_experiment(
        data,
        cfg,
        manifest=pinned,
        journal=journal,
        api=api,
        split='dev',
        repeats=1,
        deadline_seconds=1,
    )
    assert third['rows'][0]['usage']['amount_micros'] == 5


@pytest.mark.asyncio
async def test_unknown_create_receipt_is_not_replayed_or_released(tmp_path):
    import httpx

    class UncertainAPI(CostedAPI):
        async def create_run(self, body, *, key):
            self.created.append(key)
            raise httpx.ReadTimeout('unknown acceptance')

    data, cfg = dataset(), budgeted_config(tmp_path)
    cfg['experiment_budget_micros'] = 5
    api = UncertainAPI(None)
    journal = Journal(tmp_path / 'experiment/journal.jsonl')
    for _ in range(2):
        result = await run_experiment(
            data,
            cfg,
            manifest=manifest(data, cfg),
            journal=journal,
            api=api,
            split='dev',
            repeats=1,
            deadline_seconds=1,
        )
        assert result['complete'] is False
    assert len(api.created) == 1
    assert sum(e.get('event') == 'case_budget_reserved' for e in journal.events()) == 1
    assert not any(e.get('event') == 'case_budget_released' for e in journal.events())


@pytest.mark.asyncio
async def test_crash_after_reservation_reuses_it_without_double_charge(tmp_path):
    from workbench_agent.evaluation.budget import ExperimentBudget
    from workbench_agent.evaluation.contracts import make_run_key

    data, cfg = dataset(), budgeted_config(tmp_path)
    cfg['experiment_budget_micros'] = 5
    pinned = manifest(data, cfg)
    journal = Journal(tmp_path / 'experiment/journal.jsonl')
    case_id = data['cases'][0]['id']
    key = make_run_key(
        pinned['experiment_id'], pinned['dataset_hash'], pinned['config_hash'], case_id, 0
    )
    assert ExperimentBudget(cfg, journal).reserve(case_id, 0, key)
    api = CostedAPI(0)
    result = await run_experiment(
        data,
        cfg,
        manifest=pinned,
        journal=journal,
        api=api,
        split='dev',
        repeats=1,
        deadline_seconds=1,
    )
    assert result['complete'] is True and len(api.created) == 4
    assert (
        sum(
            e.get('event') == 'case_budget_reserved' and e.get('request_key') == key
            for e in journal.events()
        )
        == 1
    )


@pytest.mark.asyncio
async def test_recovered_unsent_reservation_is_blocked_after_another_case_overruns(tmp_path):
    from workbench_agent.evaluation.budget import ExperimentBudget
    from workbench_agent.evaluation.contracts import make_run_key

    data, cfg = dataset(), budgeted_config(tmp_path)
    pinned = manifest(data, cfg)
    journal = Journal(tmp_path / 'experiment/journal.jsonl')
    budget = ExperimentBudget(cfg, journal)
    first, second = data['cases'][:2]
    key_a = make_run_key(
        pinned['experiment_id'], pinned['dataset_hash'], pinned['config_hash'], first['id'], 0
    )
    key_b = make_run_key(
        pinned['experiment_id'], pinned['dataset_hash'], pinned['config_hash'], second['id'], 0
    )
    assert budget.reserve(first['id'], 0, key_a)
    assert budget.reserve(second['id'], 0, key_b)
    journal.append(
        {
            'event': 'case_result',
            'case_id': first['id'],
            'repeat': 0,
            'request_key': key_a,
            'row': {
                'case_id': first['id'],
                'repeat': 0,
                'request_key': key_a,
                'run_id': 'run-a',
                'run_status': 'succeeded',
                'transport_status': 'completed',
                'usage': {
                    'amount_micros': None,
                    'currency': 'USD',
                    'cost_pending': True,
                    'usage_pending': True,
                },
            },
        }
    )
    api = CostedAPI(8)
    result = await run_experiment(
        data,
        cfg,
        manifest=pinned,
        journal=journal,
        api=api,
        split='dev',
        repeats=1,
        deadline_seconds=1,
    )
    assert api.created == []
    assert result['complete'] is False


@pytest.mark.asyncio
async def test_legacy_unknown_dispatch_blocks_new_budgeted_runs(tmp_path):
    data, cfg = dataset(), budgeted_config(tmp_path)
    pinned = manifest(data, cfg)
    journal = Journal(tmp_path / 'experiment/journal.jsonl')
    journal.append(
        {
            'event': 'create_sent',
            'request_key': 'legacy-key',
            'case_id': data['cases'][0]['id'],
            'repeat': 0,
        }
    )
    api = CostedAPI(0)
    result = await run_experiment(
        data,
        cfg,
        manifest=pinned,
        journal=journal,
        api=api,
        split='dev',
        repeats=1,
        deadline_seconds=1,
    )
    assert api.created == []
    assert result['complete'] is False


@pytest.mark.asyncio
async def test_exact_budget_boundary_allows_second_case_but_not_third(tmp_path):
    data, cfg = dataset(), budgeted_config(tmp_path)
    api = CostedAPI(5)
    result = await run_experiment(
        data,
        cfg,
        manifest=manifest(data, cfg),
        journal=Journal(tmp_path / 'experiment/journal.jsonl'),
        api=api,
        split='dev',
        repeats=1,
        deadline_seconds=1,
    )
    assert len(api.created) == 2
    assert result['complete'] is False


@pytest.mark.asyncio
async def test_confirmed_rejection_releases_once_and_is_not_retried(tmp_path):
    from workbench_agent.evaluation.api_client import APIError

    class RejectedAPI(CostedAPI):
        async def create_run(self, body, *, key):
            self.created.append(key)
            if len(self.created) == 1:
                raise APIError(422, 'INVALID_REQUEST')
            return {'run_id': f'run-{len(self.created)}'}

    data, cfg = dataset(), budgeted_config(tmp_path)
    cfg['experiment_budget_micros'] = 5
    api = RejectedAPI(0)
    journal = Journal(tmp_path / 'experiment/journal.jsonl')
    for _ in range(2):
        result = await run_experiment(
            data,
            cfg,
            manifest=manifest(data, cfg),
            journal=journal,
            api=api,
            split='dev',
            repeats=1,
            deadline_seconds=1,
        )
        assert result['complete'] is True
    assert len(api.created) == 4
    assert sum(e.get('event') == 'case_budget_released' for e in journal.events()) == 1


@pytest.mark.asyncio
async def test_usage_is_read_after_run_reaches_terminal_state(tmp_path):
    class TerminalUsage(CostedAPI):
        terminal = set()

        async def get_run(self, run_id):
            self.terminal.add(run_id)
            return await super().get_run(run_id)

        async def usage(self, run_id):
            assert run_id in self.terminal
            return await super().usage(run_id)

    data, cfg = dataset(), budgeted_config(tmp_path)
    api = TerminalUsage(0)
    result = await run_experiment(
        data,
        cfg,
        manifest=manifest(data, cfg),
        journal=Journal(tmp_path / 'experiment/journal.jsonl'),
        api=api,
        split='dev',
        repeats=1,
        deadline_seconds=1,
    )
    assert result['complete'] is True


@pytest.mark.asyncio
async def test_synthetic_engineering_run_preserves_dataset_purpose(tmp_path):
    from workbench_agent.evaluation.dataset import dataset_fingerprint

    business = dataset()
    data = {**business, 'dataset_kind': 'synthetic'}
    cfg = config(tmp_path)
    pinned = manifest(business, cfg)
    pinned['dataset_hash'] = dataset_fingerprint(data, purpose='engineering')
    pinned['purpose'] = 'engineering'
    api = CostedAPI(0)
    result = await run_experiment(
        data,
        cfg,
        manifest=pinned,
        journal=Journal(tmp_path / 'experiment/journal.jsonl'),
        api=api,
        split='dev',
        repeats=1,
        deadline_seconds=1,
    )
    assert result['complete'] is True
    assert len(api.created) == 4
