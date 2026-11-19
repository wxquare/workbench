from typing import Literal, TypedDict
from pydantic import Field
from workbench_agent.domain.identity import Role
from workbench_agent.domain.profiles import FrozenModel, VersionRef, RoleModelBinding

class RunContext(FrozenModel):
    team_id: str
    agent_id: str
    subject: str
    identity_kind: str
    agent_scope: tuple[str, ...] | None = None
    roles: tuple[Role, ...]
    accepted_at_ms: int
    source: str = 'api'
    agent_profile: VersionRef
    graph_version: str
    system_prompt: str = Field(default='', max_length=12000, exclude_if=lambda v: v == '')
    response_language: Literal['auto', 'zh', 'en'] = Field(default='auto', exclude_if=lambda v: v == 'auto')
    response_format: Literal['default', 'markdown', 'json'] = Field(default='default', exclude_if=lambda v: v == 'default')
    conversation_history_turns: int = Field(default=8, ge=0, le=8, strict=True, exclude_if=lambda v: v == 8)
    knowledge_max_age_seconds: int | None = Field(default=None, gt=0, strict=True, exclude_if=lambda value: value is None)
    default_model: VersionRef
    role_models: tuple[RoleModelBinding, ...] = ()
    skills: tuple[VersionRef, ...] = ()
    tools: tuple[VersionRef, ...] = ()
    repositories: tuple[VersionRef, ...] = ()
    workflow: VersionRef | None = Field(default=None, exclude_if=lambda value: value is None)

    def model_for(self, role: str) -> VersionRef:
        return next((binding.ref for binding in self.role_models if binding.role == role), self.default_model)


class QueryScopeRevision(TypedDict):
    """Resolved durable scope, constructed only by ContextResolver."""
    team_id: str
    scope_id: str
    run_id: str
    version: int
    scope_json: str
    hash: str
    created_by: str
    created_at: int
    updated_at: int
