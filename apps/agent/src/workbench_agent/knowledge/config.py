"""Deployment-only knowledge retrieval configuration."""

from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class KnowledgeConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True, validate_default=True)


class AllowedScope(KnowledgeConfig):
    service: str = Field(min_length=1, max_length=128, pattern=r'^[^*,\s]+$')
    environment: str = Field(min_length=1, max_length=128, pattern=r'^[^*,\s]+$')
    region: str = Field(min_length=1, max_length=128, pattern=r'^[^*,\s]+$')


class KnowledgeGrant(KnowledgeConfig):
    name: str = Field(min_length=1, max_length=128)
    team_id: UUID
    business_team: str = Field(min_length=1, max_length=128, pattern=r'^[^*,\s]+$')
    source_id: str = Field(min_length=1, max_length=128, pattern=r'^[^*,\s]+$')
    token_secret_ref: str
    allowed_scopes: tuple[AllowedScope, ...] = Field(min_length=1)

    @field_validator('token_secret_ref')
    @classmethod
    def valid_reference(cls, value):
        ref = urlsplit(value)
        if ref.scheme != 'env' or not ref.netloc or ref.path or ref.query or ref.fragment:
            raise ValueError('service token must use env:// reference')
        return value


class KnowledgeSettings(KnowledgeConfig):
    enabled: bool = False
    grants: tuple[KnowledgeGrant, ...] = ()
    public_base_url: str | None = None

    @model_validator(mode='after')
    def validate_grants(self):
        if self.enabled and not self.grants:
            raise ValueError('enabled knowledge access requires grants')
        if len({grant.name for grant in self.grants}) != len(self.grants):
            raise ValueError('duplicate knowledge grant')
        if self.public_base_url is not None:
            url = urlsplit(self.public_base_url)
            if (
                url.scheme not in {'http', 'https'}
                or not url.netloc
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise ValueError('invalid knowledge public base URL')
        return self
