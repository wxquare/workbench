"""Idempotent human reconciliation of an unknown external delivery."""

from workbench_agent.application.errors import DomainError, not_found
from workbench_agent.application.idempotency import Idempotency
from workbench_agent.delivery.reconcile import ActionReconciler


class ReconciliationService:
    def __init__(self, backend):
        self.backend = backend

    async def reconcile(self, auth, action_id, *, key):
        # Lookup happens outside a SQLite write transaction. The reconciler
        # performs its own current-authority and revision checks before commit.
        async with self.backend.create_uow().begin_read() as uow:
            action = await self.backend.actions.one(
                uow, 'actions', team_id=auth.team_id, action_id=action_id
            )
            if not action:
                raise not_found()
            run = await self.backend.runs.one(
                uow, 'runs', team_id=auth.team_id, run_id=action['run_id']
            )
            if not run:
                raise not_found()
            from workbench_agent.application.authorization import Authorization

            await Authorization(self.backend).current(uow, auth, run['agent_id'], 'operator')
            if auth.identity_kind == 'connector':
                raise DomainError('OPERATION_DENIED', 403)
            receipt = await Idempotency(self.backend).lookup(
                uow,
                auth,
                run['agent_id'],
                'action.reconcile',
                key,
                {'action_id': action_id},
            )
        if receipt:
            return receipt.body
        try:
            result = await ActionReconciler(self.backend).reconcile(auth, action_id)
        except DomainError as error:
            if error.code == 'ACTION_NOT_UNKNOWN':
                async with self.backend.create_uow().begin_write() as uow:
                    receipt = await Idempotency(self.backend).lookup(
                        uow,
                        auth,
                        run['agent_id'],
                        'action.reconcile',
                        key,
                        {'action_id': action_id},
                    )
                if receipt:
                    return receipt.body
            raise
        if result['state'] == 'unknown':
            return result
        async with self.backend.create_uow().begin_write() as uow:
            receipt = await Idempotency(self.backend).lookup(
                uow,
                auth,
                run['agent_id'],
                'action.reconcile',
                key,
                {'action_id': action_id},
            )
            if receipt:
                return receipt.body
        # ActionReconciler already wrote the durable fact. Persist the HTTP
        # idempotency receipt in its own short transaction.
        async with self.backend.create_uow().begin_write() as uow:
            previous = await self.backend.configuration.one(
                uow,
                'idempotency_records',
                team_id=auth.team_id,
                subject=auth.subject,
                agent_id=run['agent_id'],
                route='action.reconcile',
                key=key,
            )
            now = await self.backend.clock.now_ms(uow)
            from hashlib import sha256
            from workbench_agent.application.idempotency import canonical
            from uuid import uuid4

            payload = {'action_id': action_id}
            request_hash = sha256(canonical(payload).encode()).hexdigest()
            values = {
                'request_hash': request_hash,
                'response_ref': canonical(result),
                'status_code': 202,
                'expires_at': now + 72 * 3600 * 1000,
                'updated_at': now,
            }
            if previous:
                await self.backend.configuration.change(
                    uow,
                    'idempotency_records',
                    {'record_id': previous['record_id']},
                    values,
                )
            else:
                await self.backend.configuration.insert(
                    uow,
                    'idempotency_records',
                    dict(
                        team_id=auth.team_id,
                        record_id=str(uuid4()),
                        subject=auth.subject,
                        agent_id=run['agent_id'],
                        route='action.reconcile',
                        key=key,
                        created_at=now,
                        **values,
                    ),
                )
        return result
