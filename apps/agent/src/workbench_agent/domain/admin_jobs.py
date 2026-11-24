from dataclasses import dataclass

ADMIN_JOB_KINDS = frozenset(
    {'skill_import', 'repo_sync', 'model_probe', 'tool_probe', 'tool_discovery', 'artifact_upload'}
)


@dataclass(frozen=True)
class AdminAuth:
    team_id: str
    agent_id: str
    subject: str
    can_manage: bool


@dataclass(frozen=True)
class AdminJob:
    job_id: str
    team_id: str
    agent_id: str
    kind: str
    status: str
    result_ref: str | None = None


AdminJobView = AdminJob


@dataclass(frozen=True)
class ClaimedAdminJob:
    job_id: str
    team_id: str
    agent_id: str
    kind: str
    payload: str
    worker_id: str
    revision: int
    attempt: int
    lease_until: int
