from dataclasses import dataclass

from .identity import Principal
from .scope import QueryScope


@dataclass(frozen=True)
class AccessPolicy:
    version: str
    tools: frozenset[str]
    scopes: frozenset[QueryScope]


class ScopePolicy:
    def allows(
        self,
        principal: Principal,
        tool: str,
        scope: QueryScope,
        source_scopes: frozenset[QueryScope],
    ) -> bool:
        return (
            tool in principal.policy.tools
            and scope in principal.policy.scopes
            and scope in source_scopes
        )
