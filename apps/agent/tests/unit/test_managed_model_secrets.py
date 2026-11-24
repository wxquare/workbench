import pytest
from cryptography.fernet import Fernet

from workbench_agent.application.errors import DomainError


def test_managed_model_secret_is_encrypted_and_scoped(tmp_path):
    from workbench_agent.connectors.managed_model_secrets import ManagedModelSecrets

    store = ManagedModelSecrets(tmp_path, Fernet.generate_key().decode())
    ref = store.put(
        'private-model-key',
        team_id='team',
        agent_id='agent',
        profile_id='profile',
        base_url='https://api.deepseek.com',
    )
    assert ref.startswith('secret://managed-model/')
    assert b'private-model-key' not in next(tmp_path.iterdir()).read_bytes()
    assert (
        store.resolve(
            ref,
            team_id='team',
            agent_id='agent',
            profile_id='profile',
            base_url='https://api.deepseek.com',
        )
        == 'private-model-key'
    )
    for scope in (
        {'team_id': 'other'},
        {'agent_id': 'other'},
        {'profile_id': 'other'},
        {'base_url': 'https://example.com'},
    ):
        values = (
            dict(
                team_id='team',
                agent_id='agent',
                profile_id='profile',
                base_url='https://api.deepseek.com',
            )
            | scope
        )
        with pytest.raises(DomainError):
            store.resolve(ref, **values)


def test_managed_model_secret_rejects_unapproved_endpoint(tmp_path):
    from workbench_agent.connectors.managed_model_secrets import ManagedModelSecrets

    store = ManagedModelSecrets(tmp_path, Fernet.generate_key().decode())
    with pytest.raises(DomainError):
        store.put(
            'private-model-key',
            team_id='team',
            agent_id='agent',
            profile_id='profile',
            base_url='https://example.com',
        )
    assert list(tmp_path.iterdir()) == []


def test_wrong_master_key_cannot_read_existing_credential(tmp_path):
    from workbench_agent.connectors.managed_model_secrets import ManagedModelSecrets

    first = ManagedModelSecrets(tmp_path, Fernet.generate_key().decode())
    ref = first.put(
        'private-model-key',
        team_id='team',
        agent_id='agent',
        profile_id='profile',
        base_url='https://api.deepseek.com',
    )
    other = ManagedModelSecrets(tmp_path, Fernet.generate_key().decode())
    assert not other.available(ref)
    with pytest.raises(DomainError):
        other.resolve(
            ref,
            team_id='team',
            agent_id='agent',
            profile_id='profile',
            base_url='https://api.deepseek.com',
        )


def test_rejects_credential_directory_accessible_to_other_users(tmp_path):
    from workbench_agent.connectors.managed_model_secrets import ManagedModelSecrets

    root = tmp_path / 'shared'
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    store = ManagedModelSecrets(root, Fernet.generate_key().decode())
    with pytest.raises(DomainError):
        store.put(
            'private-model-key',
            team_id='team',
            agent_id='agent',
            profile_id='profile',
            base_url='https://api.deepseek.com',
        )


def test_shared_profile_credentials_are_team_and_profile_scoped_and_immutable(tmp_path):
    from workbench_agent.connectors.managed_model_secrets import ManagedModelSecrets

    store = ManagedModelSecrets(tmp_path, Fernet.generate_key().decode())
    scope = dict(
        team_id='team',
        agent_id='*',
        profile_id='shared-profile',
        base_url='https://api.deepseek.com',
    )
    first = store.put('old-key', **scope)
    second = store.put('new-key', **scope)
    assert first != second
    runtime_scope = dict(scope, agent_id='agent-1')
    assert store.resolve(first, **runtime_scope) == 'old-key'
    assert store.resolve(second, **dict(runtime_scope, agent_id='agent-2')) == 'new-key'
    with pytest.raises(DomainError):
        store.resolve(second, **dict(runtime_scope, team_id='other-team'))
    with pytest.raises(DomainError):
        store.resolve(second, **dict(runtime_scope, profile_id='other-profile'))


def test_managed_key_requires_deployment_agent_and_endpoint_authorization():
    from workbench_agent.runtime.models.factory import ModelCredentialPolicy

    policy = ModelCredentialPolicy(
        [
            {
                'team_id': 'team',
                'agent_ids': ['allowed-agent'],
                'base_url': 'https://api.deepseek.com',
                'secret_ref': 'env://EXISTING_KEY',
            }
        ]
    )
    policy.require_managed('https://api.deepseek.com', 'team', 'allowed-agent')
    with pytest.raises(DomainError):
        policy.require_managed('https://api.deepseek.com', 'team', 'other-agent')
    with pytest.raises(DomainError):
        policy.require_managed('https://example.com', 'team', 'allowed-agent')
