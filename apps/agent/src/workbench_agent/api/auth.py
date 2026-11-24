"""Bearer verification boundary. Caller team/role headers are never consumed."""

import asyncio
from uuid import UUID
from urllib.parse import urlsplit
import jwt
from fastapi import Request
from workbench_agent.domain.identity import AuthContext, VerifiedIdentity
from workbench_agent.application.authorization import Authorization
from workbench_agent.application.errors import DomainError, not_found


def unauthenticated():
    return DomainError('UNAUTHENTICATED', 401)


class OIDCVerifier:
    def __init__(self, issuer, audience, *, jwks=None, jwks_url=None):
        parsed = urlsplit(issuer)
        if (
            parsed.scheme != 'https'
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not audience
        ):
            raise ValueError('OIDC requires a configured HTTPS issuer and audience')
        self.issuer, self.audience, self.jwks = issuer, audience, jwks
        self.jwks_url = jwks_url
        self.client = None
        if jwks_url:
            endpoint = urlsplit(jwks_url)
            if (
                endpoint.scheme != 'https'
                or not endpoint.hostname
                or endpoint.username
                or endpoint.password
            ):
                raise ValueError('JWKS requires HTTPS')
            self.client = jwt.PyJWKClient(jwks_url, timeout=5)

    async def _key(self, token):
        if self.jwks is not None:
            header = jwt.get_unverified_header(token)
            if header.get('alg') != 'RS256':
                raise unauthenticated()
            candidates = [key for key in self.jwks['keys'] if key.get('kid') == header.get('kid')]
            if len(candidates) != 1:
                raise unauthenticated()
            return jwt.PyJWK.from_dict(candidates[0], algorithm='RS256').key
        if self.client is None:
            import httpx

            async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
                response = await client.get(
                    self.issuer.rstrip('/') + '/.well-known/openid-configuration'
                )
                response.raise_for_status()
                discovery = response.json()
            if discovery.get('issuer') != self.issuer:
                raise unauthenticated()
            uri = discovery['jwks_uri']
            endpoint = urlsplit(uri)
            if (
                endpoint.scheme != 'https'
                or not endpoint.hostname
                or endpoint.username
                or endpoint.password
            ):
                raise unauthenticated()
            self.client = jwt.PyJWKClient(uri, timeout=5)
        return (await asyncio.to_thread(self.client.get_signing_key_from_jwt, token)).key

    async def verify(self, token) -> VerifiedIdentity:
        try:
            if len(token) > 16384:
                raise unauthenticated()
            key = await self._key(token)
            claims = jwt.decode(
                token,
                key,
                algorithms=['RS256'],
                issuer=self.issuer,
                audience=self.audience,
                options={'require': ['exp', 'iss', 'aud', 'sub']},
            )
            subject = claims['sub']
            if not isinstance(subject, str) or not 1 <= len(subject) <= 128:
                raise unauthenticated()
            return VerifiedIdentity(
                subject=subject,
                issuer=self.issuer,
                team_hint=claims.get('team_id'),
                connector_id=claims.get('connector_id'),
            )
        except DomainError:
            raise
        except Exception:
            # Verification/network/parser exceptions must never echo token material.
            raise unauthenticated() from None


class DevelopmentVerifier:
    async def verify(self, token):
        if not token.startswith('development:'):
            raise unauthenticated()
        subject = token.removeprefix('development:')
        team_hint = None
        candidate, separator, remainder = subject.partition(':')
        if separator:
            try:
                if str(UUID(candidate)) == candidate:
                    team_hint, subject = candidate, remainder
            except ValueError:
                pass
        if not 1 <= len(subject) <= 128:
            raise unauthenticated()
        return VerifiedIdentity(subject=subject, issuer='development', team_hint=team_hint)


class IdentityResolver:
    def __init__(self, backend, verifier, *, connectors=None):
        self.backend, self.verifier = backend, verifier
        self.connectors = connectors or {}
        self.authorization = Authorization(backend)

    async def resolve(self, token) -> AuthContext:
        identity = await self.verifier.verify(token)
        connector = self.connectors.get(identity.connector_id) if identity.connector_id else None
        service_subject = any(
            item['subject'] == identity.subject for item in self.connectors.values()
        )
        if identity.connector_id or service_subject:
            if (
                not connector
                or connector['subject'] != identity.subject
                or not connector['agent_ids']
            ):
                raise unauthenticated()
        async with self.backend.create_uow().begin_read() as uow:
            team_hint = connector['team_id'] if connector else identity.team_hint
            scope = {'subject': identity.subject, 'status': 'active'}
            if team_hint:
                scope['team_id'] = team_hint
            # Two rows suffice to reject ambiguous unhinted subjects; selection precedes limit.
            rows = await self.backend.configuration.rows(uow, 'team_members', limit=2, **scope)
            if len(rows) != 1:
                raise not_found()
            auth = AuthContext(
                team_id=rows[0]['team_id'],
                subject=identity.subject,
                roles=(rows[0]['role'],),
                identity_kind='connector'
                if connector
                else 'development'
                if identity.issuer == 'development'
                else 'user',
                agent_scope=tuple(connector['agent_ids']) if connector else None,
            )
            return await self.authorization.current(uow, auth)


async def authenticate(request: Request):
    value = request.headers.get('Authorization', '')
    scheme, _, token = value.partition(' ')
    if scheme.lower() != 'bearer' or not token or ' ' in token:
        raise unauthenticated()
    return await request.app.state.identity.resolve(token)
