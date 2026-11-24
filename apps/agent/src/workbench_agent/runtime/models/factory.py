"""Construct provider models only from trusted, scoped deployment bindings."""

from langchain_deepseek import ChatDeepSeek
from workbench_agent.application.errors import DomainError


class ModelCredentialPolicy:
    """Deployment-owned exact endpoint/reference pairs bound to team/agents."""

    def __init__(self, bindings=()):
        self.bindings = tuple(
            b.model_dump() if hasattr(b, 'model_dump') else dict(b) for b in bindings
        )

    def require(self, model, team_id, agent_id):
        if not any(
            b['team_id'] == team_id
            and agent_id in b['agent_ids']
            and b['base_url'] == model.base_url
            and b['secret_ref'] == model.secret_ref
            for b in self.bindings
        ):
            raise DomainError('MODEL_PROFILE_UNAVAILABLE', 409)

    def require_managed(self, base_url, team_id, agent_id):
        if not any(
            b['team_id'] == team_id and agent_id in b['agent_ids'] and b['base_url'] == base_url
            for b in self.bindings
        ):
            raise DomainError('MODEL_PROFILE_UNAVAILABLE', 409)


class ModelFactory:
    def __init__(self, policy=None, managed_secrets=None):
        self.policy = policy or ModelCredentialPolicy()
        self.managed_secrets = managed_secrets

    def build(self, profile, secrets, *, team_id=None, agent_id=None):
        model = profile.configuration
        try:
            if model.secret_ref.startswith('secret://managed-model/'):
                if self.managed_secrets is None:
                    raise DomainError('MODEL_CREDENTIAL_UNAVAILABLE', 409)
                self.policy.require_managed(model.base_url, team_id, agent_id)
                secret = self.managed_secrets.resolve(
                    model.secret_ref,
                    team_id=team_id,
                    agent_id=agent_id,
                    profile_id=profile.profile_id,
                    base_url=model.base_url,
                )
            else:
                self.policy.require(model, team_id, agent_id)
                secret = secrets.resolve(model.secret_ref)
            return ChatDeepSeek(
                model=model.model_id,
                base_url=model.base_url,
                api_key=secret,
                temperature=model.temperature,
                timeout=model.request_timeout_seconds,
                max_tokens=model.max_output_tokens,
                max_retries=0,
                streaming=model.streaming,
                extra_body=model.provider_options,
            )
        except DomainError:
            raise
        except Exception:
            raise DomainError('MODEL_PROFILE_UNAVAILABLE', 409) from None
