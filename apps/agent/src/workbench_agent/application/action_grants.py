"""Database-authoritative, deployment-managed policy for external actions.

Only the explicit local maintenance command synchronizes this policy. Runtime
processes consult it inside their own UoW; no process-local grant restores a
revocation. Policy revision is part of each approval's bound policy hash.
"""

import json
from workbench_agent.application.errors import DomainError
from workbench_agent.application.idempotency import canonical, digest


class ActionGrantService:
    def __init__(self, backend):
        self.backend = backend

    async def sync(self, grants, tool_permissions, connector_identities, *, expected_revision):
        if type(expected_revision) is not int or expected_revision < 0:
            raise DomainError('INVALID_REVISION', 422)
        from workbench_agent.settings import ServiceSettings

        value = ServiceSettings.model_validate(
            {
                'action_grants': grants,
                'tool_permissions': tool_permissions,
                'connector_identities': connector_identities,
            }
        )
        document = {
            'grants': [g.model_dump(mode='json') for g in value.action_grants],
            'tool_permissions': value.tool_permissions,
            'connector_identities': value.connector_identities,
        }
        try:
            encoded = canonical(document)
        except (ValueError, TypeError):
            raise DomainError('INVALID_ACTION_POLICY', 422) from None
        if len(encoded.encode()) > 1048576:
            raise DomainError('INVALID_ACTION_POLICY', 422)
        hashed = digest(document)
        async with self.backend.create_uow().begin_write() as uow:
            old = await self.backend.actions.policy(uow)
            if (old['revision'] if old else 0) != expected_revision:
                raise DomainError('REVISION_CONFLICT', 409)
            if old and old['policy_hash'] == hashed and old['policy_json'] == encoded:
                return old['revision']
            now = await self.backend.clock.now_ms(uow)
            if old:
                revision = old['revision'] + 1
                if not await self.backend.actions.update_policy(
                    uow,
                    old['revision'],
                    {
                        'revision': revision,
                        'policy_hash': hashed,
                        'policy_json': encoded,
                        'updated_at': now,
                    },
                ):
                    raise DomainError('REVISION_CONFLICT', 409)
            else:
                revision = 1
                await self.backend.actions.insert_policy(
                    uow,
                    {
                        'policy_id': 'deployment',
                        'revision': revision,
                        'policy_hash': hashed,
                        'policy_json': encoded,
                        'updated_at': now,
                    },
                )
            from uuid import uuid4

            teams = {item['team_id'] for item in document['grants']}
            if old:
                try:
                    teams.update(
                        item['team_id'] for item in json.loads(old['policy_json'])['grants']
                    )
                except (ValueError, KeyError, TypeError):
                    raise DomainError('INVALID_ACTION_POLICY', 409) from None
            for team_id in teams:
                await self.backend.configuration.insert(
                    uow,
                    'audit_logs',
                    dict(
                        audit_id=str(uuid4()),
                        team_id=team_id,
                        actor='deployment-policy',
                        object='action-policy',
                        operation='action.policy.sync',
                        decision='allowed',
                        trace=canonical({'policy_hash': hashed, 'revision': revision}),
                        created_at=now,
                        updated_at=now,
                    ),
                )
            return revision


async def matched_action_grant(backend, uow, context, ref, args):
    row = await backend.actions.policy(uow)
    if not row:
        raise DomainError('OPERATION_DENIED', 403)
    try:
        policy = json.loads(row['policy_json'])
        if digest(policy) != row['policy_hash'] or set(policy) != {
            'grants',
            'tool_permissions',
            'connector_identities',
        }:
            raise ValueError()
        found = []
        for grant in policy['grants']:
            if (
                not isinstance(grant, dict)
                or set(grant) != {'team_id', 'agent_id', 'binding', 'approver', 'arguments'}
                or not isinstance(grant['approver'], str)
                or not grant['approver'].strip()
                or not isinstance(grant['arguments'], dict)
            ):
                continue
            if (
                grant['team_id'] == context.team_id
                and grant['agent_id'] == context.agent_id
                and grant['binding'] == ref.model_dump(mode='json')
                and canonical(grant['arguments']) == canonical(args)
            ):
                found.append(grant)
        if len(found) != 1:
            raise ValueError()
        return found[0], digest({'grant': found[0], 'policy_revision': row['revision']}), policy
    except (ValueError, TypeError, KeyError):
        raise DomainError('OPERATION_DENIED', 403) from None
