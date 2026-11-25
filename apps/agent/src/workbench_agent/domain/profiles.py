"""Immutable published references; drafts are validated before publication."""

from workbench_agent.runtime.graphs.ids import GraphId
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from workbench_agent.settings import ModelProfile

MODEL_ROLES = frozenset({'router', 'knowledge', 'diagnosis', 'code', 'summary', 'evaluation'})


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')


class VersionRef(FrozenModel):
    resource_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)
    content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')


class RoleModelBinding(FrozenModel):
    role: Literal['router', 'knowledge', 'diagnosis', 'code', 'summary', 'evaluation']
    ref: VersionRef


class ProfileBudgetLimits(FrozenModel):
    # Active-execution time; separate from the Run total-lifetime wall_deadline_at.
    wall_seconds: int | None = Field(default=None, gt=0, strict=True)
    model_calls: int | None = Field(default=None, gt=0, strict=True)
    tool_calls: int | None = Field(default=None, gt=0, strict=True)
    tokens: int | None = Field(default=None, gt=0, strict=True)


class ProfileDraft(FrozenModel):
    default_model: VersionRef
    description: str = Field(default='', max_length=1000, exclude_if=lambda v: v == '')
    system_prompt: str = Field(default='', max_length=12000, exclude_if=lambda v: v == '')
    response_language: Literal['auto', 'zh', 'en'] = Field(
        default='auto', exclude_if=lambda v: v == 'auto'
    )
    response_format: Literal['default', 'markdown', 'json'] = Field(
        default='default', exclude_if=lambda v: v == 'default'
    )
    conversation_history_turns: int = Field(
        default=8, ge=0, le=8, strict=True, exclude_if=lambda v: v == 8
    )
    allowed_modes: tuple[Literal['auto', 'qa', 'rca', 'code', 'sop', 'inspection'], ...] = Field(
        default=('auto', 'qa', 'rca', 'code', 'sop', 'inspection'),
        exclude_if=lambda v: v == ('auto', 'qa', 'rca', 'code', 'sop', 'inspection'),
    )
    budget_limits: ProfileBudgetLimits | None = Field(default=None, exclude_if=lambda v: v is None)
    role_models: tuple[RoleModelBinding, ...] = ()
    knowledge_max_age_seconds: int | None = Field(
        default=None, gt=0, strict=True, exclude_if=lambda value: value is None
    )
    graph_id: str = Field(default=GraphId.CONVERSATION_MULTI_TURN, min_length=1, max_length=128)
    skills: tuple[VersionRef, ...] = ()
    tools: tuple[VersionRef, ...] = ()
    repositories: tuple[VersionRef, ...] = ()

    @model_validator(mode='after')
    def unique_roles(self):
        if len({binding.role for binding in self.role_models}) != len(self.role_models):
            raise ValueError('duplicate role binding')
        return self


class RuntimeProfile(ProfileDraft):
    profile_id: str
    agent_id: str
    team_id: str
    version: str
    content_hash: str

    @property
    def ref(self):
        return VersionRef(
            resource_id=self.profile_id, version=self.version, content_hash=self.content_hash
        )


class ModelProfileVersion(FrozenModel):
    model_version_id: str
    profile_id: str
    name: str
    version: str
    content_hash: str
    configuration_json: str

    @property
    def ref(self):
        return VersionRef(
            resource_id=self.model_version_id, version=self.version, content_hash=self.content_hash
        )

    @property
    def configuration(self):
        # Return a fresh validated copy; mutable provider options never alias the snapshot.
        return ModelProfile.model_validate_json(self.configuration_json)

    @property
    def model_id(self):
        return self.configuration.model_id

    @property
    def secret_ref(self):
        return self.configuration.secret_ref
