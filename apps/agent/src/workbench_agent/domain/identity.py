"""Trusted identity snapshots. Current membership is always checked separately."""

from typing import Literal
from pydantic import BaseModel, ConfigDict

Role = Literal['viewer', 'operator', 'maintainer', 'admin']
ROLE_LEVEL = {'viewer': 0, 'operator': 1, 'maintainer': 2, 'admin': 3}


class AuthContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    team_id: str
    subject: str
    roles: tuple[Role, ...]
    identity_kind: Literal['user', 'connector', 'development'] = 'user'
    agent_scope: tuple[str, ...] | None = None


class VerifiedIdentity(BaseModel):
    model_config = ConfigDict(frozen=True)
    subject: str
    issuer: str
    team_hint: str | None = None
    connector_id: str | None = None
