"""Explicit CAS policy mapping signed sources and exact scopes to fixed proposals."""

import json
from sqlalchemy import select, insert, update
from workbench_agent.application.errors import DomainError
from workbench_agent.application.idempotency import canonical, digest
from workbench_agent.domain.profiles import VersionRef
from workbench_agent.persistence import schema


def validate_rule(rule):
    try:
        if set(rule) != {'source', 'scope', 'binding', 'arguments', 'approver'}:
            raise ValueError()
        source, scope, args = rule['source'], rule['scope'], rule['arguments']
        if set(source) != {
            'team_id',
            'agent_id',
            'connector_id',
            'identity_id',
            'subject',
            'service',
            'environment',
            'region',
            'rule',
        }:
            raise ValueError()
        if set(scope) != {'service', 'environment', 'region', 'start_at', 'end_at'}:
            raise ValueError()
        if not all(
            isinstance(v, str) and v.strip() and v == v.strip() and len(v) <= 128
            for v in source.values()
        ):
            raise ValueError()
        if not all(
            isinstance(v, str) and v.strip() and v == v.strip() and len(v) <= 128
            for v in scope.values()
        ):
            raise ValueError()
        if any(source[k] != scope[k] for k in ('service', 'environment', 'region')):
            raise ValueError()
        if not isinstance(args, dict) or any(args.get(k) != v for k, v in scope.items()):
            raise ValueError()
        if (
            not isinstance(rule['approver'], str)
            or not rule['approver'].strip()
            or rule['approver'] != rule['approver'].strip()
            or len(rule['approver']) > 128
        ):
            raise ValueError()
        ref = VersionRef.model_validate(rule['binding'])
        if ref.model_dump(mode='json') != rule['binding']:
            raise ValueError()
        canonical(rule)
        return rule
    except Exception:
        raise DomainError('INVALID_PROPOSAL_POLICY', 422) from None


class SyntheticProposalPolicy:
    def __init__(self, backend):
        self.backend = backend

    async def sync(self, rules, *, expected_revision):
        if (
            type(expected_revision) is not int
            or expected_revision < 0
            or not isinstance(rules, list)
        ):
            raise DomainError('INVALID_PROPOSAL_POLICY', 422)
        values = sorted([validate_rule(x) for x in rules], key=canonical)
        if (
            len({canonical(v) for v in values}) != len(values)
            or len(canonical(values).encode()) > 1048576
        ):
            raise DomainError('INVALID_PROPOSAL_POLICY', 422)
        table = schema.synthetic_proposal_policy
        async with self.backend.create_uow().begin_write() as uow:
            row = (
                (await uow.execute(select(table).where(table.c.policy_id == 'deployment')))
                .mappings()
                .first()
            )
            if (row['revision'] if row else 0) != expected_revision:
                raise DomainError('PROPOSAL_POLICY_CONFLICT', 409)
            now = await self.backend.clock.now_ms(uow)
            data = dict(
                revision=expected_revision + 1,
                policy_json=canonical(values),
                policy_hash=digest(values),
                updated_at=now,
            )
            if row:
                changed = await uow.execute(
                    update(table)
                    .where(table.c.policy_id == 'deployment', table.c.revision == expected_revision)
                    .values(**data)
                )
                if changed.rowcount != 1:
                    raise DomainError('PROPOSAL_POLICY_CONFLICT', 409)
            else:
                await uow.execute(insert(table).values(policy_id='deployment', **data))
            return data['revision']


async def matching_rule(uow, source, scope, *, with_policy=False):
    table = schema.synthetic_proposal_policy
    row = (
        (await uow.execute(select(table).where(table.c.policy_id == 'deployment')))
        .mappings()
        .first()
    )
    try:
        rules = json.loads(row['policy_json']) if row else None
        if not isinstance(rules, list) or digest(rules) != row['policy_hash']:
            raise ValueError()
        matches = [
            validate_rule(r) for r in rules if r.get('source') == source and r.get('scope') == scope
        ]
        if len(matches) != 1:
            raise ValueError()
        return (matches[0], row['revision'], row['policy_hash']) if with_policy else matches[0]
    except (ValueError, TypeError, KeyError, AttributeError, DomainError):
        raise DomainError('OPERATION_DENIED', 403) from None


async def bound_rule(backend, uow, context, run_id, scope_id, *, with_policy=False):
    from types import SimpleNamespace
    from workbench_agent.application.synthetic_source_policy import require_bound_source
    from workbench_agent.incidents.synthetic import source_scope

    await require_bound_source(uow, context, run_id)
    source, event, scope, matches = await source_scope(
        backend, uow, SimpleNamespace(team_id=context.team_id, run_id=run_id)
    )
    if not matches or not scope or scope['scope_id'] != scope_id:
        raise DomainError('OPERATION_DENIED', 403)
    selector = {
        'team_id': context.team_id,
        'agent_id': context.agent_id,
        'connector_id': source['connector_id'],
        'identity_id': source['identity_id'],
        'subject': context.subject,
        'service': event.service,
        'environment': event.environment,
        'region': event.region,
        'rule': event.rule,
    }
    matched = await matching_rule(
        uow, selector, json.loads(scope['scope_json']), with_policy=with_policy
    )
    if not with_policy:
        return matched
    source_policy = (
        (
            await uow.execute(
                select(schema.synthetic_source_policy).where(
                    schema.synthetic_source_policy.c.policy_id == 'deployment'
                )
            )
        )
        .mappings()
        .one()
    )
    return (*matched, source_policy['revision'], source_policy['policy_hash'])
