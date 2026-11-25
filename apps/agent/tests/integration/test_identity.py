import time
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from workbench_agent.api.auth import OIDCVerifier, IdentityResolver
from workbench_agent.application.errors import DomainError


@pytest.fixture
def signed_identity():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
    jwk['kid'] = 'fixture'
    verifier = OIDCVerifier('https://identity.example', 'workbench-agents', jwks={'keys': [jwk]})

    def token(_omit=(), _headers=None, **claims):
        payload = {
            'iss': 'https://identity.example',
            'aud': 'workbench-agents',
            'sub': 'development-admin',
            'exp': int(time.time()) + 120,
            'roles': ['admin'],
            **claims,
        }
        for claim in _omit:
            payload.pop(claim, None)
        return jwt.encode(
            payload, private, algorithm='RS256', headers={'kid': 'fixture', **(_headers or {})}
        )

    return verifier, token


async def test_oidc_verifies_signature_issuer_audience_and_expiry(signed_identity):
    verifier, token = signed_identity
    assert (await verifier.verify(token())).subject == 'development-admin'
    for claims in ({'iss': 'wrong'}, {'aud': 'wrong'}, {'exp': 1}, {'sub': ''}):
        with pytest.raises(DomainError) as error:
            await verifier.verify(token(**claims))
        assert error.value.status == 401
    wrong = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(
        {
            'iss': 'https://identity.example',
            'aud': 'workbench-agents',
            'sub': 'development-admin',
            'exp': int(time.time()) + 120,
        },
        wrong,
        algorithm='RS256',
        headers={'kid': 'fixture'},
    )
    with pytest.raises(DomainError):
        await verifier.verify(forged)


async def test_oidc_roles_and_team_hint_cannot_grant_membership(platform, signed_identity):
    verifier, token = signed_identity
    resolver = IdentityResolver(platform.backend, verifier)
    await platform.teams.set_member(
        platform.auth, 'development-admin', 'viewer', 'active', expected_revision=0, key='demote'
    )
    auth = await resolver.resolve(token(roles=['admin']))
    assert auth.roles == ('viewer',)
    with pytest.raises(DomainError):
        await resolver.resolve(token(team_id=platform.other_auth.team_id))
    with pytest.raises(DomainError):
        await resolver.resolve(token(sub='unknown'))


async def test_raw_team_and_role_headers_are_not_identity(client, platform):
    response = await client.get(
        '/v1/agents',
        headers={'Authorization': '', 'X-Team-ID': platform.auth.team_id, 'X-Role': 'admin'},
    )
    assert response.status_code == 401


async def test_connector_identity_is_bound_to_configured_team_and_agent(platform, signed_identity):
    verifier, token = signed_identity
    await platform.teams.set_member(
        platform.auth, 'service:alerts', 'operator', 'active', expected_revision=0, key='connector'
    )
    resolver = IdentityResolver(
        platform.backend,
        verifier,
        connectors={
            'alerts': {
                'subject': 'service:alerts',
                'team_id': platform.auth.team_id,
                'agent_ids': (platform.agent_id,),
            }
        },
    )
    auth = await resolver.resolve(token(sub='service:alerts', connector_id='alerts'))
    assert auth.identity_kind == 'connector'
    assert auth.agent_scope == (platform.agent_id,)
    with pytest.raises(DomainError):
        await resolver.resolve(token(sub='service:alerts', connector_id='other'))
    with pytest.raises(DomainError):
        await resolver.resolve(token(sub='development-admin', connector_id='alerts'))


async def test_production_api_uses_verified_bearer_and_ignores_spoofed_headers(
    platform, signed_identity, monkeypatch
):
    from workbench_agent.api.app import create_app
    from httpx import ASGITransport, AsyncClient
    from workbench_agent.settings import ServiceSettings

    verifier, token = signed_identity
    monkeypatch.setenv('WORKBENCH_TEST_ISSUER', 'https://identity.example')
    # Identity uses pytest's temporary SQLite fixture; production path policy is tested separately.
    monkeypatch.setattr(
        'workbench_agent.persistence.readiness.persistent_path_errors', lambda settings: []
    )
    service = ServiceSettings(auth_mode='oidc', oidc_issuer_ref='env://WORKBENCH_TEST_ISSUER')
    settings = platform.settings.model_copy(
        update={'environment': 'production', 'service': service}
    )
    app = create_app(settings, backend=platform.backend, verifier=verifier)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get(
            '/v1/agents',
            headers={
                'Authorization': 'Bearer ' + token(),
                'X-Team-ID': platform.other_auth.team_id,
                'X-Role': 'admin',
            },
        )
        assert response.status_code == 200
        assert [item['agent_id'] for item in response.json()['items']] == [platform.agent_id]
        assert (
            await client.get(
                '/v1/agents', headers={'Authorization': 'Bearer development:development-admin'}
            )
        ).status_code == 401


async def test_connector_cannot_drop_identity_marker_to_escape_agent_scope(
    platform, signed_identity
):
    verifier, token = signed_identity
    await platform.teams.set_member(
        platform.auth, 'service:alerts', 'admin', 'active', expected_revision=0, key='service'
    )
    resolver = IdentityResolver(
        platform.backend,
        verifier,
        connectors={
            'alerts': {
                'subject': 'service:alerts',
                'team_id': platform.auth.team_id,
                'agent_ids': (platform.agent_id,),
            }
        },
    )
    with pytest.raises(DomainError):
        await resolver.resolve(token(sub='service:alerts'))
    auth = await resolver.resolve(token(sub='service:alerts', connector_id='alerts'))
    with pytest.raises(DomainError) as error:
        await platform.profiles.publish(auth, platform.agent_id, 0, key='forbidden')
    assert error.value.status == 403


async def test_production_composition_rejects_verifier_from_another_issuer(
    platform, signed_identity, monkeypatch
):
    from workbench_agent.api.app import create_app
    from workbench_agent.settings import ServiceSettings

    verifier, _ = signed_identity
    monkeypatch.setenv('WORKBENCH_TEST_ISSUER', 'https://another-issuer.example')
    settings = platform.settings.model_copy(
        update={
            'environment': 'production',
            'service': ServiceSettings(
                auth_mode='oidc', oidc_issuer_ref='env://WORKBENCH_TEST_ISSUER'
            ),
        }
    )
    with pytest.raises(ValueError):
        create_app(settings, backend=platform.backend, verifier=verifier)


@pytest.mark.parametrize('claim', ['exp', 'sub', 'iss', 'aud'])
async def test_oidc_rejects_missing_required_claim(signed_identity, claim):
    verifier, token = signed_identity
    with pytest.raises(DomainError) as error:
        await verifier.verify(token(_omit=(claim,)))
    assert error.value.status == 401


async def test_oidc_ignores_token_supplied_jwks_location(signed_identity):
    verifier, token = signed_identity
    identity = await verifier.verify(
        token(_headers={'jku': 'https://attacker.invalid/jwks', 'jwk': {'kty': 'oct'}})
    )
    assert identity.subject == 'development-admin'


@pytest.mark.parametrize(
    'connector',
    [
        {'subject': 'service:alerts', 'team_id': 'not-a-uuid', 'agent_ids': []},
        {
            'subject': '',
            'team_id': '9d1e3b8a-8450-5a34-a806-c5732a34a161',
            'agent_ids': ['1626b294-3417-55c6-a2a1-01bdb6047800'],
        },
    ],
)
def test_invalid_connector_configuration_is_rejected_before_readiness(connector):
    from pydantic import ValidationError
    from workbench_agent.settings import ServiceSettings

    with pytest.raises(ValidationError):
        ServiceSettings(connector_identities={'alerts': connector})


async def test_verified_team_hint_matches_membership_beyond_inventory_page(
    platform, signed_identity
):
    from uuid import UUID

    verifier, token = signed_identity
    selected = None
    async with platform.backend.create_uow().begin_write() as uow:
        for number in range(1, 102):
            team_id = str(UUID(int=number + 1000))
            await platform.backend.configuration.insert(
                uow, 'teams', {'team_id': team_id, 'name': f'team-{number}', 'status': 'active'}
            )
            await platform.backend.configuration.insert(
                uow,
                'team_members',
                {
                    'team_id': team_id,
                    'member_id': str(UUID(int=number)),
                    'subject': 'many-team-user',
                    'role': 'viewer',
                    'status': 'active',
                },
            )
            selected = team_id
    resolver = IdentityResolver(platform.backend, verifier)
    auth = await resolver.resolve(token(sub='many-team-user', team_id=selected))
    assert auth.team_id == selected
    with pytest.raises(DomainError) as error:
        await resolver.resolve(token(sub='many-team-user'))
    assert error.value.status == 404
