import ipaddress
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from workbench_mcp_server.application.queries import RuntimeSource
from workbench_mcp_server.domain.identity import Principal
from workbench_mcp_server.domain.policy import AccessPolicy
from workbench_mcp_server.domain.scope import QueryScope


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class ScopeConfig(StrictModel):
    team: str
    environment: str
    region: str
    service: str
    source_id: str

    @model_validator(mode="after")
    def valid_scope(self):
        self.domain()
        return self

    def domain(self):
        return QueryScope(**self.model_dump())


def secret_ref(ref):
    if not re.fullmatch(r"env://[A-Za-z_][A-Za-z0-9_]*", ref):
        raise ValueError("Only env:// references are supported")


class ServerConfig(StrictModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    local_development: bool = False
    stdio_principal: str | None = None
    allowed_hosts: list[str] = Field(default_factory=lambda: ["127.0.0.1:*", "localhost:*"])


class PrincipalConfig(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    token_secret_ref: str = Field(repr=False)
    tools: list[Literal["knowledge_search", "metrics_query", "logs_search", "trace_get"]] = Field(
        min_length=1
    )
    scopes: list[ScopeConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_ref(self):
        secret_ref(self.token_secret_ref)
        return self


class TemplateParameter(StrictModel):
    type: Literal["string", "integer"]
    allowed: list[str | int] = Field(min_length=1)


class MetricTemplate(StrictModel):
    parameters: dict[str, TemplateParameter] = Field(default_factory=dict)


class SourceConfig(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    kind: Literal["knowledge", "metrics", "logs", "trace"]
    adapter: Literal["normalized-http-v1", "dp-knowledge-v2"]
    endpoint: str = Field(repr=False)
    auth_secret_ref: str = Field(repr=False)
    binding_id: str = Field(min_length=1, max_length=128)
    scopes: list[ScopeConfig] = Field(min_length=1)
    timeout_seconds: float = Field(default=20, gt=0, le=30)
    templates: dict[str, MetricTemplate] = Field(default_factory=dict)
    scope_enforced: bool = False
    isolation: Literal["upstream-enforced", "exclusive-source"] = "upstream-enforced"
    allowed_source_hosts: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_source(self):
        secret_ref(self.auth_secret_ref)
        if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in self.endpoint):
            raise ValueError("Invalid source endpoint")
        parsed = urlsplit(self.endpoint)
        parsed.port  # Validate numeric/range constraints before reporting configuration ready.
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Invalid source endpoint")
        if any(s.source_id != self.id for s in self.scopes):
            raise ValueError("Source scope mismatch")
        if self.adapter == "dp-knowledge-v2" and self.kind != "knowledge":
            raise ValueError("Knowledge adapter requires knowledge source")
        if not self.scope_enforced:
            raise ValueError("An upstream scope enforcement guarantee is required")
        if self.adapter == "dp-knowledge-v2" and self.isolation != "exclusive-source":
            raise ValueError("DP knowledge V2 requires a verified exclusive source")
        if self.isolation == "exclusive-source" and len(self.scopes) != 1:
            raise ValueError("Exclusive sources must bind exactly one resource tuple")
        if self.kind == "metrics" and not self.templates:
            raise ValueError("Metrics sources require explicit templates")
        return self


class Limits(StrictModel):
    deadline_seconds: float = Field(default=20, gt=0, le=30)
    global_concurrency: int = Field(default=4, ge=1, le=4)
    principal_concurrency: int = Field(default=2, ge=1, le=2)
    response_bytes: int = Field(default=262144, ge=4096, le=262144)
    upstream_bytes: int = Field(default=2097152, ge=1024, le=2097152)
    window_seconds: int = Field(default=3600, ge=1, le=3600)


class AuditConfig(StrictModel):
    redact_fields: list[str] = Field(
        default_factory=lambda: ["password", "token", "authorization", "cookie", "secret"]
    )


class Settings(StrictModel):
    version: str = "v1"
    server: ServerConfig = Field(default_factory=ServerConfig)
    principals: list[PrincipalConfig] = Field(min_length=1)
    sources: list[SourceConfig] = Field(min_length=1)
    limits: Limits = Field(default_factory=Limits)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    cursor_secret_ref: str | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def consistent(self):
        ids = [s.id for s in self.sources]
        pids = [p.id for p in self.principals]
        if len(set(ids)) != len(ids) or len(set(pids)) != len(pids):
            raise ValueError("Duplicate binding")
        if self.server.stdio_principal and self.server.stdio_principal not in pids:
            raise ValueError("Unknown stdio principal")
        if self.cursor_secret_ref:
            secret_ref(self.cursor_secret_ref)
        if any(s.kind == "logs" for s in self.sources) and not self.cursor_secret_ref:
            raise ValueError("Log pagination requires cursor signing secret")
        for p in self.principals:
            if any(s.source_id not in ids for s in p.scopes):
                raise ValueError("Unknown source in principal scope")
        for source in self.sources:
            parsed = urlsplit(source.endpoint)
            local = parsed.hostname == "localhost"
            try:
                local |= ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                pass
            if parsed.scheme == "http" and not (self.server.local_development and local):
                raise ValueError("HTTPS required except for explicit loopback development")
        return self

    def principal(self, principal_id):
        p = next(p for p in self.principals if p.id == principal_id)
        return Principal(
            p.id,
            AccessPolicy(self.version, frozenset(p.tools), frozenset(s.domain() for s in p.scopes)),
        )

    def runtime_sources(self):
        return {
            s.id: RuntimeSource(s.id, s.kind, s.binding_id, frozenset(x.domain() for x in s.scopes))
            for s in self.sources
        }


def load_settings(path: str | Path) -> Settings:
    raw = Path(path).read_bytes()
    if len(raw) > 1048576:
        raise ValueError("Configuration too large")
    try:
        value = yaml.safe_load(raw)
    except yaml.YAMLError:
        raise ValueError("Invalid configuration syntax") from None
    return Settings.model_validate(value)
