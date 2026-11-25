"""Real caller-UoW budget operations shared by integration scenarios."""

import pytest

from workbench_agent.domain.usage import ReservationRequest, Reservation


async def assert_dispatch_transaction_rolls_back(platform, monkeypatch, budget, dispatch):
    """Inject faults after each budget write and verify no reservation survives."""
    before = await platform.count_rows('usage_reservations')
    for method in ('reserve', 'mark_sent'):
        original = getattr(budget, method)

        async def fail_after_write(*args, **kwargs):
            await original(*args, **kwargs)
            raise RuntimeError('injected transaction failure')

        with monkeypatch.context() as patch:
            patch.setattr(budget, method, fail_after_write)
            with pytest.raises(RuntimeError, match='injected transaction failure'):
                await dispatch(method)
        assert await platform.count_rows('usage_reservations') == before


async def reserve_usage(
    platform,
    guard,
    *,
    invocation_id,
    kind='expert',
    model_attempts=None,
    tool_attempts=None,
    token_upper_bound=None,
):
    tool = kind == 'tool'
    request = ReservationRequest(
        invocation_id=invocation_id,
        kind=kind,
        model_attempts=(0 if tool else 1) if model_attempts is None else model_attempts,
        tool_attempts=(1 if tool else 0) if tool_attempts is None else tool_attempts,
        token_upper_bound=(0 if tool else 100) if token_upper_bound is None else token_upper_bound,
    )
    async with platform.backend.create_uow().begin_write() as uow:
        return await platform.budgets.reserve(guard, request, uow=uow)


def is_reserved(result):
    return isinstance(result, Reservation) and result.status == 'reserved'
