"""Shared deployment configuration. Secret references remain references except DB URL."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from sqlalchemy.engine import make_url
from workbench_agent.knowledge.config import KnowledgeSettings

SCHEMA_VERSION = "dod-storage-agent-v1"
CHECKPOINT_CODEC_VERSION = "dod-checkpoint-v1"
MIN_SQLITE_VERSION = (3, 51, 3)


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, validate_default=True)


def require_sqlite_absolute_path(database_url: str) -> Path:
    try:
        url = make_url(database_url)
    except Exception:
        raise ValueError("invalid database URL") from None
    if url.drivername != "sqlite+aiosqlite" or not url.database or url.host or url.username or url.password or url.query:
        raise ValueError("backend/driver mismatch or unsupported SQLite URI")
    if url.database == ":memory:" or not Path(url.database).is_absolute():
        raise ValueError("persistent absolute SQLite path required")
    return Path(url.database).resolve()


def secret_reference(value: str) -> str:
    ref = urlsplit(value)
    if ref.scheme not in {"env", "secret"} or not ref.netloc or ref.query or ref.fragment:
        raise ValueError("secret_ref must be an env:// or secret:// reference")
    return value


def resolve_env_reference(reference: str) -> str:
    ref = urlsplit(secret_reference(reference))
    if ref.scheme != "env" or ref.path:
        raise ValueError("database URL requires an env:// reference")
    value = os.environ.get(ref.netloc)
    if not value:
        raise ValueError("database URL environment reference is unset")
    return value


def asset_root_from_env() -> Path:
    value = os.environ.get("WORKBENCH_ASSET_ROOT")
    if not value:
        raise ValueError("asset_root requires WORKBENCH_ASSET_ROOT or explicit configuration")
    return Path(value).resolve() if Path(value).is_absolute() else _invalid_asset_root()


def _invalid_asset_root():
    raise ValueError("asset_root must be an absolute directory")


class ModelCredentialBinding(ConfigModel):
    team_id: str
    agent_ids: tuple[str, ...] = Field(min_length=1)
    base_url: str
    secret_ref: str

    @field_validator('team_id')
    @classmethod
    def valid_team(cls, value):
        from uuid import UUID
        if str(UUID(value)) != value: raise ValueError('canonical UUID required')
        return value

    @field_validator('agent_ids')
    @classmethod
    def valid_agents(cls, values):
        from uuid import UUID
        if any(str(UUID(value)) != value for value in values): raise ValueError('canonical UUID required')
        return values

    @field_validator('secret_ref')
    @classmethod
    def valid_ref(cls,value): return secret_reference(value)

    @field_validator('base_url')
    @classmethod
    def valid_url(cls,value):
        parsed=urlsplit(value)
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('credential binding endpoint requires HTTPS')
        return value

class MCPConnectionBinding(ConfigModel):
    server: str = Field(min_length=1,max_length=128)
    credential_scope: str = Field(min_length=1,max_length=128)
    team_id: str
    agent_id: str
    endpoint: str
    secret_ref: str
    read_tools: tuple[str,...] = Field(min_length=1)
    probe_requests: dict[str,dict] = Field(default_factory=dict)
    scope_grants: tuple[dict,...] = ()
    allow_loopback_http: bool = False

    @model_validator(mode="after")
    def validate_probes(self):
        if set(self.probe_requests)-set(self.read_tools): raise ValueError("probe tool is not authorized")
        from ipaddress import ip_address
        parsed=urlsplit(self.endpoint)
        if parsed.scheme=='http':
            try: loopback=ip_address(parsed.hostname).is_loopback
            except ValueError: loopback=False
            if not self.allow_loopback_http or not loopback: raise ValueError('HTTP MCP requires explicitly enabled numeric loopback')
        return self

    @field_validator('team_id','agent_id')
    @classmethod
    def valid_id(cls,value):
        from uuid import UUID
        if str(UUID(value))!=value: raise ValueError('canonical UUID required')
        return value

    @field_validator('secret_ref')
    @classmethod
    def valid_ref(cls,value): return secret_reference(value)

    @field_validator('endpoint')
    @classmethod
    def valid_endpoint(cls,value):
        parsed=urlsplit(value)
        if parsed.scheme not in ('https','http') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('invalid MCP endpoint')
        return value

class SyntheticIngressBinding(ConfigModel):
    """Only local test protocol; not a production SeaTalk or alert adapter."""
    protocol: Literal['synthetic-chat-v1','synthetic-alert-v1','synthetic-alert-v2']
    team_id: str
    agent_id: str
    secret_ref: str
    actors: dict[str,str] = Field(default_factory=dict)
    actor_subject: str | None = None
    identity_id: str | None = None
    allowed_scopes: list[tuple[str,str,str,str]] = Field(default_factory=list)

    @model_validator(mode='after')
    def validate_binding(self):
        import re
        from uuid import UUID
        for value in (self.team_id,self.agent_id):
            if str(UUID(value)) != value: raise ValueError('canonical UUID required')
        if not re.fullmatch(r'env://[A-Za-z_][A-Za-z0-9_]*',self.secret_ref):
            raise ValueError('synthetic ingress requires env secret reference')
        if self.protocol == 'synthetic-chat-v1':
            if not self.actors or self.actor_subject or any(not k or not v or len(k)>128 or len(v)>128 for k,v in self.actors.items()):
                raise ValueError('chat requires explicit actor mapping')
        elif not self.actor_subject or self.actors or len(self.actor_subject)>128:
            raise ValueError('alert requires fixed subject')
        if self.protocol == 'synthetic-alert-v2':
            if not self.identity_id or not self.identity_id.strip() or self.identity_id != self.identity_id.strip() or len(self.identity_id)>128: raise ValueError('v2 requires an exact identity id')
            if not self.allowed_scopes or any(any(not value.strip() or value != value.strip() for value in scope) for scope in self.allowed_scopes):
                raise ValueError('v2 requires explicit scopes')
        elif self.allowed_scopes or self.identity_id:
            raise ValueError('identity and scopes are only valid for synthetic alert v2')
        return self

class ActionGrantBinding(ConfigModel):
    resource_id: str = Field(min_length=1,max_length=128)
    version: str = Field(min_length=1,max_length=128)
    content_hash: str = Field(pattern=r'^[0-9a-f]{64}$')

class ActionDeploymentGrant(ConfigModel):
    team_id: str
    agent_id: str
    binding: ActionGrantBinding
    approver: str = Field(min_length=1,max_length=128)
    arguments: dict = Field(min_length=1)

    @field_validator('team_id','agent_id')
    @classmethod
    def canonical_ids(cls,value):
        from uuid import UUID
        if str(UUID(value)) != value: raise ValueError('canonical UUID required')
        return value

    @field_validator('approver')
    @classmethod
    def explicit_human(cls,value):
        if not value.strip() or value!=value.strip(): raise ValueError('named approver required')
        return value

    @field_validator('arguments')
    @classmethod
    def bounded_arguments(cls,value):
        import json
        if len(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode())>65536:
            raise ValueError('action arguments too large')
        return value

class ServiceSettings(ConfigModel):
    name: str = "workbench-agents"
    default_timezone: str = "Asia/Shanghai"
    auth_mode: Literal["development", "oidc"] = "development"
    oidc_issuer_ref: str | None = None
    oidc_audience: str = "workbench-agents"
    oidc_jwks_url: str | None = None
    connector_identities: dict[str, dict] = Field(default_factory=dict)
    synthetic_ingress: dict[str,SyntheticIngressBinding] = Field(default_factory=dict)
    model_credential_bindings: tuple[ModelCredentialBinding, ...] = ()
    managed_model_endpoints: tuple[str, ...] = ('https://api.deepseek.com',)
    mcp_connections: tuple[MCPConnectionBinding, ...] = ()
    tool_permissions: dict = Field(default_factory=dict)
    action_grants: tuple[ActionDeploymentGrant,...] = ()
    rca_metric_template: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator('managed_model_endpoints')
    @classmethod
    def valid_managed_model_endpoints(cls, values):
        for value in values:
            parsed = urlsplit(value)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError('managed model endpoints require credential-free HTTPS URLs')
        return values

    @field_validator("connector_identities")
    @classmethod
    def valid_connector_identities(cls, value: dict) -> dict:
        from uuid import UUID
        for name, binding in value.items():
            if not 1 <= len(name) <= 128 or set(binding) != {"subject", "team_id", "agent_ids"}:
                raise ValueError("invalid connector identity binding")
            if not isinstance(binding["subject"], str) or not 1 <= len(binding["subject"]) <= 128:
                raise ValueError("connector requires explicit service subject")
            ids = binding["agent_ids"]
            if not isinstance(ids, (list, tuple)) or not ids:
                raise ValueError("connector requires explicit agent allowlist")
            for resource in (binding["team_id"], *ids):
                if not isinstance(resource, str) or str(UUID(resource)) != resource:
                    raise ValueError("canonical connector scope UUID required")
        return value

    @field_validator("default_timezone")
    @classmethod
    def timezone_exists(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError("invalid IANA timezone") from None
        return value

    @field_validator("oidc_issuer_ref")
    @classmethod
    def issuer_reference(cls, value: str | None) -> str | None:
        return secret_reference(value) if value else value


class ModelProfile(ConfigModel):
    name: str = "deepseek-dod-default"
    provider: Literal["deepseek"] = "deepseek"
    adapter: Literal["deepseek"] = "deepseek"
    protocol: Literal["openai_chat_completions"] = "openai_chat_completions"
    base_url: str = "https://api.deepseek.com"
    model_id: str = "deepseek-flash"
    secret_ref: str = "env://DEEPSEEK_API_KEY"
    temperature: float = 0
    request_timeout_seconds: int = Field(default=60, gt=0)
    max_output_tokens: int = Field(default=4096, gt=0)
    max_context_tokens: int = Field(default=64000, gt=0)
    streaming: bool = True
    sdk_max_retries: Literal[0] = 0
    invocation_max_attempts: int = Field(default=3, ge=1, le=3)
    structured_output_strategy: Literal["json_mode"] = "json_mode"
    provider_options: dict = Field(default_factory=lambda: {"thinking": {"type": "disabled"}})

    @field_validator("secret_ref")
    @classmethod
    def validate_reference(cls, value: str) -> str:
        return secret_reference(value)

    @field_validator("base_url")
    @classmethod
    def valid_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("model endpoint requires a credential-free HTTPS URL")
        return value

    @field_validator("provider_options")
    @classmethod
    def disable_thinking(cls, value: dict) -> dict:
        if value != {"thinking": {"type": "disabled"}}:
            raise ValueError("thinking must be disabled")
        return value


class LLMSettings(ConfigModel):
    bootstrap_policy: Literal["create_if_missing"] = "create_if_missing"
    default_profile: str = "deepseek-dod-default"
    profiles: list[ModelProfile] = Field(default_factory=lambda: [ModelProfile()])

    @model_validator(mode="after")
    def default_exists(self) -> Self:
        names = [profile.name for profile in self.profiles]
        if self.default_profile not in names or len(set(names)) != len(names):
            raise ValueError("default model profile must exist with unique profile names")
        return self


class SQLiteSettings(ConfigModel):
    journal_mode: Literal["WAL"] = "WAL"
    synchronous: Literal["FULL"] = "FULL"
    foreign_keys: Literal[True] = True
    busy_timeout_ms: Literal[5000] = 5000
    wal_autocheckpoint_pages: Literal[1000] = 1000


class RepositorySource(ConfigModel):
    team_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    repo_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,127}$")
    path: Path

    @field_validator("path")
    @classmethod
    def absolute_repository_path(cls, value):
        if not value.is_absolute():
            raise ValueError("repository path must be absolute")
        return value


class S3Deployment(ConfigModel):
    endpoint_ref: str
    access_key_ref: str
    secret_key_ref: str
    bucket: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
    region: str = "us-east-1"
    allow_http: bool = False

    @field_validator("endpoint_ref", "access_key_ref", "secret_key_ref")
    @classmethod
    def references(cls, value):
        return secret_reference(value)


class RunnerDeploymentBinding(ConfigModel):
    alias: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    team_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    snapshot_id: str = Field(min_length=1, max_length=128)
    commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    image: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    snapshot_volume: str = Field(pattern=r"^dod-snapshot-[0-9a-f]{32}$")
    script: str = Field(min_length=1, max_length=512)
    script_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    interpreter: Literal["python", "python3"] = "python3"


class StorageSettings(ConfigModel):
    backend: Literal["sqlite"] = "sqlite"
    database_url: SecretStr | None = Field(default=None, repr=False, exclude=True)
    database_url_ref: str | None = None
    persistent_root: Path | None = None
    schema_version: Literal["dod-storage-agent-v1"] = SCHEMA_VERSION
    checkpoint_store: Literal["portable"] = "portable"
    checkpoint_codec_version: Literal["dod-checkpoint-v1"] = CHECKPOINT_CODEC_VERSION
    operation_timeout_seconds: Literal[5] = 5
    sqlite: SQLiteSettings = Field(default_factory=SQLiteSettings)
    object_endpoint_ref: str = "env://WORKBENCH_S3_ENDPOINT"
    object_bucket: str = "dod-artifacts"
    skill_object_root: Path | None = None
    upload_staging_root: Path | None = None
    s3: S3Deployment | None = None
    repository_sources: list[RepositorySource] = Field(default_factory=list)
    runner_bindings: list[RunnerDeploymentBinding] = Field(default_factory=list)
    docker_binary: Path | None = None
    docker_host: str = "unix:///var/run/docker.sock"
    runner_daemon_id: str | None = Field(default=None,pattern=r"^[a-zA-Z0-9_.:-]{1,128}$")
    runner_identity_mode: Literal["docker_engine", "podman_volume"] = "docker_engine"
    skill_git_repositories: dict[str, Path] = Field(default_factory=dict)

    @field_validator("docker_host")
    @classmethod
    def local_docker_host(cls, value):
        from workbench_agent.runner.service import validate_docker_host
        return validate_docker_host(value)

    @field_validator("skill_git_repositories")
    @classmethod
    def absolute_skill_git_repositories(cls, value):
        import re
        if any(not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", key) or not path.is_absolute() for key, path in value.items()):
            raise ValueError("skill Git repositories require safe names and absolute paths")
        return value

    @field_validator("skill_object_root", "upload_staging_root", "docker_binary")
    @classmethod
    def absolute_skill_object_root(cls, value):
        if value is not None and not value.is_absolute():
            raise ValueError("skill object root must be absolute")
        return value
    object_auth_ref: str = "secret://dod/object-storage"

    @field_validator("database_url_ref", "object_endpoint_ref", "object_auth_ref")
    @classmethod
    def validate_ref(cls, value: str | None) -> str | None:
        return secret_reference(value) if value else value

    @field_validator("persistent_root")
    @classmethod
    def absolute_root(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("persistent_root must be absolute")
        return value.resolve() if value else None

    @model_validator(mode="after")
    def resolve_database(self) -> Self:
        if self.database_url is not None and self.database_url_ref:
            raise ValueError("provide database_url or database_url_ref, never both")
        if self.database_url_ref:
            self.database_url = SecretStr(resolve_env_reference(self.database_url_ref))
        if self.database_url is None:
            raise ValueError("database URL reference is required")
        require_sqlite_absolute_path(self.database_url.get_secret_value())
        if self.runner_identity_mode == 'podman_volume':
            import re
            if self.runner_daemon_id is None or not re.fullmatch(r'podman:[0-9a-f]{32}', self.runner_daemon_id):
                raise ValueError('podman runner requires pre-provisioned identity volume')
        elif self.runner_daemon_id and self.runner_daemon_id.startswith('podman:'):
            raise ValueError('podman identity requires podman_volume mode')
        if self.runner_bindings and (self.runner_daemon_id is None or self.skill_object_root is None):
            raise ValueError('runner bindings require daemon identity and snapshot object root')
        return self

    @property
    def database_path(self) -> Path:
        assert self.database_url is not None
        return require_sqlite_absolute_path(self.database_url.get_secret_value())


class RedisSettings(ConfigModel):
    url_ref: str = "env://WORKBENCH_REDIS_URL"
    required_for_admission: Literal[False] = False

    @field_validator("url_ref")
    @classmethod
    def redis_ref(cls, value: str) -> str:
        return secret_reference(value)


class JobSettings(ConfigModel):
    backend: Literal["database"] = "database"
    poll_interval_ms: Literal[1000] = 1000
    lease_seconds: Literal[45] = 45
    heartbeat_seconds: Literal[10] = 10
    lease_scan_seconds: Literal[5] = 5
    max_segment_attempts: Literal[3] = 3


class WorkerSettings(ConfigModel):
    concurrency: Literal[2] = 2
    supported_graph_versions: list[str] = Field(default_factory=lambda: ["dod.v1", "dod.v2", "dod.v3"])
    expert_concurrency_per_run: Literal[2] = 2
    tool_concurrency_per_run: Literal[4] = 4


class StreamSettings(ConfigModel):
    flush_interval_ms: Literal[250] = 250
    flush_max_chars: Literal[256] = 256
    heartbeat_seconds: Literal[15] = 15
    event_retention_days: Literal[7] = 7
    client_buffer_events: Literal[100] = 100


class RunnerSettings(ConfigModel):
    image_profile: str = "python312-tools-v1"
    script_timeout_seconds: Literal[60] = 60
    cpu_limit: Literal[2] = 2
    memory_mib: Literal[2048] = 2048
    max_artifact_mib: Literal[20] = 20


class BootstrapSettings(ConfigModel):
    # Explicit local seed identity. Production additionally requires a verified OIDC token.
    team_id: str = "9d1e3b8a-8450-5a34-a806-c5732a34a161"
    agent_id: str = "1626b294-3417-55c6-a2a1-01bdb6047800"
    team_name: str = "development"
    agent_name: str = "default"
    admin_subject: str = Field(default="development-admin", min_length=1, max_length=128)

    @field_validator("team_id", "agent_id")
    @classmethod
    def canonical_uuid(cls, value: str) -> str:
        from uuid import UUID
        if str(UUID(value)) != value:
            raise ValueError("canonical lowercase UUID required")
        return value

class LoggingSettings(ConfigModel):
    level: Literal['DEBUG', 'INFO', 'WARNING', 'ERROR'] = 'INFO'
    format: Literal['json'] = 'json'


class TracingSettings(ConfigModel):
    enabled: bool = False
    sample_ratio: float = Field(default=1.0, ge=0.0, le=1.0)


class LangfuseSettings(ConfigModel):
    enabled: bool = False
    base_url_ref: str = 'env://LANGFUSE_BASE_URL'
    public_key_ref: str = 'env://LANGFUSE_PUBLIC_KEY'
    secret_key_ref: str = 'env://LANGFUSE_SECRET_KEY'
    content_mode: Literal['metadata_only', 'redacted_content'] = 'metadata_only'
    max_input_bytes: int = Field(default=65536, ge=1, le=65536)
    max_output_bytes: int = Field(default=65536, ge=1, le=65536)

    @field_validator('base_url_ref', 'public_key_ref', 'secret_key_ref')
    @classmethod
    def valid_reference(cls, value):
        parsed = urlsplit(secret_reference(value))
        if parsed.scheme != 'env' or parsed.path:
            raise ValueError('observability credentials require env references')
        return value


class ObservabilitySettings(ConfigModel):
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    tracing: TracingSettings = Field(default_factory=TracingSettings)
    langfuse: LangfuseSettings = Field(default_factory=LangfuseSettings)

    @model_validator(mode='after')
    def langfuse_requires_tracing(self):
        if self.langfuse.enabled and not self.tracing.enabled:
            raise ValueError('Langfuse requires tracing')
        return self


class Settings(ConfigModel):
    bootstrap: BootstrapSettings = Field(default_factory=BootstrapSettings)
    environment: Literal["development", "test", "production"] = "development"
    asset_root: Path = Field(default_factory=asset_root_from_env)
    frontend_dist_root: Path | None = None
    service: ServiceSettings = Field(default_factory=ServiceSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    storage: StorageSettings
    redis: RedisSettings = Field(default_factory=RedisSettings)
    jobs: JobSettings = Field(default_factory=JobSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    stream: StreamSettings = Field(default_factory=StreamSettings)
    runner: RunnerSettings = Field(default_factory=RunnerSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    knowledge: KnowledgeSettings = Field(default_factory=KnowledgeSettings)

    @field_validator("asset_root")
    @classmethod
    def absolute_asset_root(cls, value: Path) -> Path:
        if not value.is_absolute() or not value.is_dir():
            _invalid_asset_root()
        return value.resolve()

    @field_validator("frontend_dist_root")
    @classmethod
    def absolute_frontend_dist_root(cls, value: Path | None) -> Path | None:
        if value is not None:
            if not value.is_absolute():
                raise ValueError("frontend_dist_root must be an absolute directory")
            return value.resolve()
        return None

    @model_validator(mode="after")
    def production_identity(self) -> Self:
        if self.environment == "production":
            if self.service.auth_mode != "oidc" or not self.service.oidc_issuer_ref:
                raise ValueError("production identity requires OIDC configuration")
            if self.knowledge.public_base_url and urlsplit(self.knowledge.public_base_url).scheme != 'https':
                raise ValueError('production knowledge public URL requires HTTPS')
        return self


def load_settings(path: Path | str) -> Settings:
    """All process entry points must use this parser; no cwd resource discovery."""
    with Path(path).open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError("configuration must be a YAML mapping")
    if frontend_root := os.environ.get("WORKBENCH_FRONTEND_DIST_ROOT"):
        data["frontend_dist_root"] = frontend_root
    if root := os.environ.get("WORKBENCH_PERSISTENT_ROOT"):
        storage = data.setdefault("storage", {})
        if not isinstance(storage, dict):
            raise ValueError("storage configuration must be a YAML mapping")
        storage["persistent_root"] = root
    if root := os.environ.get("WORKBENCH_SKILL_OBJECT_ROOT"):
        storage = data.setdefault("storage", {})
        if not isinstance(storage, dict):
            raise ValueError("storage configuration must be a YAML mapping")
        storage["skill_object_root"] = root
    return Settings.model_validate(data)
