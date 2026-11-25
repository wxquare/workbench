"""Integer micro-unit budget reservations for evaluation experiments."""

from __future__ import annotations


def case_upper_bound(limits: dict, pricing: dict) -> int:
    currency = pricing.get('currency')
    version = pricing.get('pricing_version')
    input_price = pricing.get('input_micros_per_million')
    output_price = pricing.get('output_micros_per_million')
    if (
        not isinstance(currency, str)
        or len(currency) != 3
        or not isinstance(version, str)
        or not version.strip()
        or version != version.strip()
        or type(input_price) is not int
        or type(output_price) is not int
        or input_price < 0
        or output_price < 0
    ):
        raise ValueError('known pricing is required for amount budgeting')
    calls = limits.get('model_calls')
    tokens = limits.get('tokens')
    if type(calls) is not int or type(tokens) is not int or calls < 1 or tokens < 1:
        raise ValueError('model and token limits are required')
    # The token limit is global. Allow each call's input and output charge to
    # round up independently to a whole micro-unit.
    rate = max(input_price, output_price)
    if rate == 0:
        return 0
    rounded_total = (tokens * rate + 999_999) // 1_000_000
    return rounded_total + min(2 * calls, tokens) - 1


def reserve_case_budget(manifest: dict, usage: dict, upper_bound: dict) -> dict:
    budget = upper_bound.get('amount_micros')
    currency = upper_bound.get('currency')
    spent = usage.get('known_costs') or {}
    if type(budget) is not int or budget < 0:
        raise ValueError('unknown budget')
    if not isinstance(currency, str):
        raise ValueError('unknown budget currency')
    if any(key != currency for key in spent):
        raise ValueError('mixed budget currencies')
    used = spent.get(currency, 0)
    if usage.get('usage_pending') or usage.get('cost_pending'):
        used += manifest.get('reserved_upper_bound_micros', 0)
    next_bound = upper_bound.get('case_upper_bound_micros')
    if type(next_bound) is not int or next_bound < 0:
        raise ValueError('unknown case upper bound')
    allowed = used + next_bound <= budget
    return {
        'allowed': allowed,
        'currency': currency,
        'used_micros': used,
        'reserved_micros': next_bound if allowed else 0,
        'remaining_micros': max(0, budget - used - next_bound),
    }


class ExperimentBudget:
    """Rebuild per-case reservations from the durable experiment journal."""

    def __init__(self, config, journal):
        self.journal = journal
        self.limit = config.get('experiment_budget_micros')
        self.entries = {}
        self.legacy_dispatch = False
        self.currency = None
        self.upper_bound = None
        self.pricing_version = None
        if self.limit is None:
            return
        pricing = config.get('pricing') or {}
        tool_price = pricing.get('tool_call_micros')
        tool_calls = config.get('limits', {}).get('tool_calls')
        if (
            type(self.limit) is not int
            or self.limit <= 0
            or pricing.get('scope') != 'all_run_models_and_tools'
            or type(tool_price) is not int
            or tool_price < 0
            or type(tool_calls) is not int
            or tool_calls < 0
        ):
            raise ValueError('complete model and tool cost ceilings are required')
        self.currency = pricing.get('currency')
        self.pricing_version = pricing.get('pricing_version')
        if (
            not isinstance(self.currency, str)
            or len(self.currency) != 3
            or not self.currency.isascii()
            or not self.currency.isalpha()
            or self.currency != self.currency.upper()
        ):
            raise ValueError('known budget currency is required')
        self.upper_bound = case_upper_bound(config['limits'], pricing) + tool_calls * tool_price
        dispatched = set()
        for event in journal.events():
            kind = event.get('event')
            key = event.get('request_key')
            if kind == 'case_budget_reserved':
                if key in self.entries or not isinstance(key, str):
                    raise ValueError('invalid experiment budget reservation')
                if (
                    event.get('currency') != self.currency
                    or event.get('pricing_version') != self.pricing_version
                    or event.get('amount_micros') != self.upper_bound
                ):
                    raise ValueError('experiment budget reservation changed')
                self.entries[key] = {'bound': self.upper_bound, 'state': 'reserved'}
            elif kind == 'case_budget_settled':
                entry = self.entries.get(key)
                amount = event.get('amount_micros')
                if (
                    entry is None
                    or entry['state'] == 'released'
                    or type(amount) is not int
                    or amount < 0
                ):
                    raise ValueError('invalid experiment budget settlement')
                if event.get('currency') != self.currency:
                    raise ValueError('experiment currency changed')
                if entry['state'] == 'settled' and entry['amount'] != amount:
                    raise ValueError('experiment budget settlement changed')
                entry.update(state='settled', amount=amount)
            elif kind == 'case_budget_released':
                entry = self.entries.get(key)
                if entry is None or entry['state'] != 'reserved':
                    raise ValueError('invalid experiment budget release')
                entry['state'] = 'released'
            elif kind == 'case_budget_currency_blocked':
                entry = self.entries.get(key)
                if entry is None or entry['state'] != 'reserved':
                    raise ValueError('invalid experiment budget currency block')
                entry['currency_blocked'] = True
            elif kind == 'create_sent' and isinstance(key, str):
                dispatched.add(key)
        self.legacy_dispatch = bool(dispatched - self.entries.keys())

    def reserve(self, case_id, repeat, request_key):
        if self.limit is None:
            return True
        existing = self.entries.get(request_key)
        used = sum(
            item.get('amount', 0)
            if item['state'] == 'settled'
            else item['bound']
            if item['state'] == 'reserved'
            else 0
            for item in self.entries.values()
        )
        overrun = any(
            item['state'] == 'settled' and item['amount'] > item['bound']
            for item in self.entries.values()
        )
        currency_blocked = any(
            item['state'] == 'reserved' and item.get('currency_blocked')
            for item in self.entries.values()
        )
        decision = reserve_case_budget(
            {},
            {'known_costs': {self.currency: used}},
            {
                'amount_micros': self.limit,
                'currency': self.currency,
                'case_upper_bound_micros': 0 if existing else self.upper_bound,
            },
        )
        allowed = (
            not self.legacy_dispatch
            and not overrun
            and not currency_blocked
            and decision['allowed']
            and (existing is None or existing['state'] == 'reserved')
        )
        if not allowed:
            self.journal.append(
                {
                    'event': 'budget_stop',
                    'case_id': case_id,
                    'repeat': repeat,
                    'request_key': request_key,
                    'budget_micros': self.limit,
                    'used_micros': used,
                    'case_upper_bound_micros': self.upper_bound,
                }
            )
            return False
        if existing is not None:
            return True
        self.journal.append(
            {
                'event': 'case_budget_reserved',
                'case_id': case_id,
                'repeat': repeat,
                'request_key': request_key,
                'amount_micros': self.upper_bound,
                'currency': self.currency,
                'pricing_version': self.pricing_version,
            }
        )
        self.entries[request_key] = {'bound': self.upper_bound, 'state': 'reserved'}
        return True

    def observe(self, row):
        if self.limit is None:
            return
        key = row.get('request_key')
        entry = self.entries.get(key)
        if not entry or entry['state'] != 'reserved':
            return
        if row.get('transport_status') == 'rejected':
            confirmed = any(
                event.get('event') == 'create_response'
                and event.get('request_key') == key
                and event.get('status') in (400, 401, 403, 404, 422)
                for event in self.journal.events()
            )
            if confirmed:
                self.journal.append({'event': 'case_budget_released', 'request_key': key})
                entry['state'] = 'released'
            return
        usage = row.get('usage')
        if row.get('transport_status') == 'completed' and isinstance(usage, dict):
            known = usage.get('known_costs')
            amount = usage.get('amount_micros')
            currency = usage.get('currency')
            invalid_costs = (
                not isinstance(known, dict)
                or any(
                    name != self.currency
                    or type(value) is not int
                    or value < 0
                    or (
                        value > entry['bound']
                        and (
                            usage.get('usage_pending') is not False
                            or usage.get('cost_pending') is not False
                        )
                    )
                    for name, value in known.items()
                )
                or (currency is not None and currency != self.currency)
                or (amount is not None and (type(amount) is not int or amount < 0))
                or (amount is not None and currency != self.currency)
                or (amount is not None and known != {self.currency: amount})
                or (
                    type(amount) is int
                    and amount > entry['bound']
                    and (
                        usage.get('usage_pending') is not False
                        or usage.get('cost_pending') is not False
                    )
                )
                or type(usage.get('usage_pending')) is not bool
                or type(usage.get('cost_pending')) is not bool
                or (
                    usage.get('usage_pending') is False
                    and usage.get('cost_pending') is False
                    and (type(amount) is not int or known != {self.currency: amount})
                )
            )
            if invalid_costs:
                if not entry.get('currency_blocked'):
                    self.journal.append(
                        {
                            'event': 'case_budget_currency_blocked',
                            'request_key': key,
                            'case_id': row.get('case_id'),
                            'repeat': row.get('repeat'),
                            'row': row,
                        }
                    )
                    entry['currency_blocked'] = True
                return
        if (
            row.get('transport_status') != 'completed'
            or not isinstance(usage, dict)
            or usage.get('usage_pending') is not False
            or usage.get('cost_pending') is not False
            or type(usage.get('amount_micros')) is not int
            or usage['amount_micros'] < 0
            or usage.get('currency') != self.currency
            or usage.get('known_costs') != {self.currency: usage['amount_micros']}
        ):
            return
        amount = usage['amount_micros']
        self.journal.append(
            {
                'event': 'case_budget_settled',
                'request_key': key,
                'amount_micros': amount,
                'currency': self.currency,
                'case_id': row.get('case_id'),
                'repeat': row.get('repeat'),
                'row': row,
            }
        )
        entry.update(state='settled', amount=amount)

    async def refresh_terminal(self, row, api):
        if self.limit is None or row.get('transport_status') != 'completed':
            return False
        entry = self.entries.get(row.get('request_key'))
        if not entry or entry['state'] != 'reserved' or not row.get('run_id'):
            return False
        import httpx
        from workbench_agent.evaluation.api_client import APIError

        try:
            row['usage'] = await api.usage(row['run_id'])
        except (APIError, httpx.TransportError, TimeoutError):
            return False
        self.observe(row)
        return True
