import pytest
from pydantic import BaseModel, StrictStr


class Answer(BaseModel):
    summary: StrictStr


@pytest.mark.parametrize(
    'text,finish', [('  ', 'stop'), ('{"summary":"ok"}', 'length'), ('{"summary":7}', 'stop')]
)
def test_invalid_json_rejected(text, finish):
    from workbench_agent.runtime.models.json_output import validate_json_output

    with pytest.raises(ValueError):
        validate_json_output(text, Answer, finish)


def test_deepseek_factory_disables_sdk_retry_and_keeps_options():
    from workbench_agent.runtime.models.factory import ModelFactory, ModelCredentialPolicy
    from workbench_agent.domain.profiles import ModelProfileVersion
    from workbench_agent.settings import ModelProfile

    config = ModelProfile()
    profile = ModelProfileVersion(
        model_version_id='v',
        profile_id='p',
        name='test',
        version='1',
        content_hash='a' * 64,
        configuration_json=config.model_dump_json(),
    )

    class Secrets:
        def resolve(self, ref):
            return 'fictional-test-key'

    policy = ModelCredentialPolicy(
        [
            {
                'team_id': 'team',
                'agent_ids': ['agent'],
                'base_url': config.base_url,
                'secret_ref': config.secret_ref,
            }
        ]
    )
    model = ModelFactory(policy).build(profile, Secrets(), team_id='team', agent_id='agent')
    assert model.max_retries == 0 and model.model_name == 'deepseek-flash'
    assert model.extra_body == {'thinking': {'type': 'disabled'}}


def test_retry_after_supports_seconds_and_http_dates():
    from workbench_agent.runtime.models.invoker import retry_after_seconds
    from datetime import datetime, timezone

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert retry_after_seconds('3', now) == 3
    assert retry_after_seconds('Thu, 01 Jan 2026 00:00:05 GMT', now) == 5
    assert retry_after_seconds('nan', now) == 0


def test_factory_denies_untrusted_endpoint_or_secret_before_secret_resolution():
    from workbench_agent.runtime.models.factory import ModelFactory, ModelCredentialPolicy
    from workbench_agent.domain.profiles import ModelProfileVersion
    from workbench_agent.application.errors import DomainError
    from workbench_agent.settings import ModelProfile

    class Secrets:
        called = False

        def resolve(self, ref):
            self.called = True
            return 'fictional-key'

    secrets = Secrets()
    policy = ModelCredentialPolicy(
        [
            {
                'team_id': 'team',
                'agent_ids': ['agent'],
                'base_url': 'https://api.deepseek.com',
                'secret_ref': 'env://DEEPSEEK_API_KEY',
            }
        ]
    )
    for config, team, agent in [
        (ModelProfile(base_url='https://attacker.example'), 'team', 'agent'),
        (ModelProfile(secret_ref='env://OTHER_SECRET'), 'team', 'agent'),
        (ModelProfile(), 'another-team', 'agent'),
        (ModelProfile(), 'team', 'another-agent'),
    ]:
        profile = ModelProfileVersion(
            model_version_id='v',
            profile_id='p',
            name='test',
            version='1',
            content_hash='a' * 64,
            configuration_json=config.model_dump_json(),
        )
        with pytest.raises(DomainError, match='MODEL_PROFILE_UNAVAILABLE'):
            ModelFactory(policy).build(profile, secrets, team_id=team, agent_id=agent)
        assert not secrets.called


def test_factory_without_deployment_bindings_fails_closed():
    from workbench_agent.runtime.models.factory import ModelFactory
    from workbench_agent.domain.profiles import ModelProfileVersion
    from workbench_agent.application.errors import DomainError
    from workbench_agent.settings import ModelProfile

    config = ModelProfile()
    profile = ModelProfileVersion(
        model_version_id='v',
        profile_id='p',
        name='test',
        version='1',
        content_hash='a' * 64,
        configuration_json=config.model_dump_json(),
    )

    class Secrets:
        def resolve(self, ref):
            raise AssertionError('must not resolve')

    with pytest.raises(DomainError, match='MODEL_PROFILE_UNAVAILABLE'):
        ModelFactory().build(profile, Secrets(), team_id='team', agent_id='agent')
