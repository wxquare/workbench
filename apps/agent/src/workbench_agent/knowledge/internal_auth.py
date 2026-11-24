"""Bind a dedicated service identity to a team and exact allowed scopes."""

import hmac

from workbench_agent.application.errors import DomainError
from workbench_agent.knowledge.models import KBScope


class KBInternalAccess:
    def __init__(self, settings, resolve_secret):
        self.settings = settings
        self.grants = []
        seen = set()
        for grant in settings.grants if settings.enabled else ():
            token = resolve_secret(grant.token_secret_ref)
            if not token or token in seen:
                raise ValueError('invalid or duplicate knowledge service credentials')
            seen.add(token)
            self.grants.append((grant, token))

    def authenticate(self, authorization):
        supplied = (
            authorization[7:] if authorization and authorization.startswith('Bearer ') else ''
        )
        matched = None
        for grant, token in self.grants:
            if supplied and hmac.compare_digest(token.encode('utf-8'), supplied.encode('utf-8')):
                matched = grant
        if matched is None:
            raise DomainError('KB_INTERNAL_UNAUTHORIZED', 401)
        return matched

    def authorize(self, authorization, raw_scope):
        matched = self.authenticate(authorization)
        allowed = any(
            all(raw_scope.get(k) == getattr(scope, k) for k in ('service', 'environment', 'region'))
            for scope in matched.allowed_scopes
        )
        if (
            raw_scope.get('team') != matched.business_team
            or raw_scope.get('source_id') != matched.source_id
            or not allowed
        ):
            raise DomainError('KB_SCOPE_DENIED', 403)
        return KBScope(
            team_id=str(matched.team_id),
            **{k: raw_scope[k] for k in ('service', 'environment', 'region')},
        )
