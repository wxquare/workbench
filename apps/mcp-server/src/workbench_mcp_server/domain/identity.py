from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .policy import AccessPolicy


@dataclass(frozen=True)
class Principal:
    id: str
    policy: "AccessPolicy"
