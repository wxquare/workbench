"""Isolated MCP sessions keyed by deployment-owned server and credential scope."""
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Callable
import hashlib
import json
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

@dataclass(frozen=True)
class TrustedConnection:
    team_id: str
    agent_id: str
    server: str
    credential_scope: str
    read_tools: frozenset[str] = frozenset()
    scope_grants: tuple = ()
    mapping_policies: tuple = ()
    tool_conversation_allowed_tools: frozenset[str] = frozenset()

class ConnectionPolicy:
    def __init__(self,bindings): self._bindings=bindings if callable(bindings) else lambda: bindings
    def require(self,team_id,agent_id,server,credential_scope,tool_name=None):
        if not any(b.team_id==team_id and b.agent_id==agent_id and b.server==server and b.credential_scope==credential_scope and (tool_name is None or tool_name in b.read_tools) for b in self._bindings()):
            raise PermissionError('MCP_CONNECTION_DENIED')

def configured_connection_policy(bindings):
    """Read deployment grants on each check, without opening a connection."""
    provider = bindings if callable(bindings) else lambda: bindings
    def current():
        return [TrustedConnection(
            b.team_id,
            b.agent_id,
            b.server,
            b.credential_scope,
            frozenset(b.read_tools),
            tuple(b.scope_grants),
            tuple(
                value.model_dump(mode='json')
                if hasattr(value, 'model_dump')
                else value
                for value in b.mapping_policies
            ),
            frozenset(b.tool_conversation_allowed_tools),
        ) for b in provider()]
    return ConnectionPolicy(current)


def permits_knowledge_evidence(policy, *, team_id, agent_id, binding, query_scope, run_id,
                               invocation_id, scope_id, upstream_scope):
    """Match an upstream knowledge source against a current deployment grant."""
    if not isinstance(policy, ConnectionPolicy) or not isinstance(upstream_scope, dict):
        return False
    correlation = {'run_id': run_id, 'invocation_id': invocation_id, 'query_scope_ref': scope_id}
    try:
        for rule in policy._bindings():
            if ((rule.team_id, rule.agent_id, rule.server, rule.credential_scope) !=
                    (team_id, agent_id, binding.server, binding.credential_scope)
                    or 'knowledge_search' not in rule.read_tools):
                continue
            for grant in rule.scope_grants:
                try:
                    mapped = build_dod_arguments('knowledge_search', {}, query_scope, grant, correlation)
                except PermissionError:
                    continue
                if mapped['scope'] == upstream_scope:
                    return True
    except (TypeError, AttributeError, KeyError):
        return False
    return False


@dataclass(frozen=True)
class ToolCatalog:
    names: tuple[str,...]
    tools: tuple[object,...]

BUSINESS_TOOLS=frozenset({'KnowledgeRetrieveV2','Common-Monitor-Query','SRE-Log-Query','SRE-Log-TraceQuery','Common-DoD-GetInfo'})

def build_business_arguments(name,args,scope,grant):
    if name not in {'SRE-Log-Query','Common-DoD-GetInfo'} or not isinstance(scope,dict) or not isinstance(grant,dict): raise PermissionError('MCP_SCOPE_MAPPING_UNAVAILABLE')
    required={'service','environment','region','start_at','end_at'}
    if set(scope)!=required or not isinstance(grant.get('team'),str) or not grant['team'].strip(): raise PermissionError('MCP_SCOPE_DENIED')
    if any(not isinstance(v,str) or not v.strip() for v in scope.values()) or ',' in scope['service'] or scope['environment'] not in ('test','uat','live'):
        raise PermissionError('MCP_SCOPE_DENIED')
    if not isinstance(args,dict): raise PermissionError('MCP_UNSAFE_ARGUMENT')
    if name=='Common-DoD-GetInfo':
        if args: raise PermissionError('MCP_UNSAFE_ARGUMENT')
    elif set(args)-{'query','limit'} or not isinstance(args.get('query'),str) or not args['query'].strip(): raise PermissionError('MCP_UNSAFE_ARGUMENT')
    from datetime import datetime
    try:
        start=datetime.strptime(scope['start_at'],'%Y-%m-%d %H:%M:%S')
        end=datetime.strptime(scope['end_at'],'%Y-%m-%d %H:%M:%S')
        if end<=start: raise ValueError()
        if 'scope' in grant:
            if scope!=grant['scope']: raise ValueError()
        else:
            fixed={k:scope[k] for k in ('service','environment','region')}
            if fixed!=grant.get('allowed_scope'): raise ValueError()
            earliest=datetime.strptime(grant['earliest'],'%Y-%m-%d %H:%M:%S')
            latest=datetime.strptime(grant['latest'],'%Y-%m-%d %H:%M:%S')
            limit=grant['max_span_seconds']
            if type(limit) is not int or limit<=0 or start<earliest or end>latest or (end-start).total_seconds()>limit: raise ValueError()
    except (ValueError,KeyError,TypeError): raise PermissionError('MCP_SCOPE_DENIED') from None
    if name=='Common-DoD-GetInfo':
        from zoneinfo import ZoneInfo
        now=datetime.now(ZoneInfo('Asia/Shanghai')).replace(tzinfo=None)
        if not start<=now<=end: raise PermissionError('MCP_CURRENT_DUTY_ONLY')
        return {'team':grant['team']}
    result=dict(args)
    result.update(team=grant['team'],env=scope['environment'],region=scope['region'],service_cmdb_name=scope['service'],begin_time=scope['start_at'],end_time=scope['end_at'])
    return result

DOD_MCP_TOOLS=frozenset({'knowledge_search','metrics_query','logs_search','trace_get'})
BUSINESS_TOOLS=BUSINESS_TOOLS | DOD_MCP_TOOLS

def build_dod_arguments(name,args,scope,grant,correlation):
    """Map only deployment-authorized values into the scoped DoD MCP contract."""
    from datetime import datetime
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    allowed={
        'knowledge_search':{'query','scene','limit'},
        'metrics_query':{'template_id','parameters','step_seconds'},
        'logs_search':{'levels','keyword','trace_id','limit','cursor'},
        'trace_get':{'trace_id'},
    }
    try:
        if name not in allowed or not isinstance(args,dict) or set(args)-allowed[name]: raise ValueError()
        if not isinstance(scope,dict) or set(scope)!={'service','environment','region','start_at','end_at'}: raise ValueError()
        if any(not isinstance(v,str) or not v.strip() for v in scope.values()): raise ValueError()
        if not isinstance(grant,dict) or grant.get('kind')!='dod-mcp-v1': raise ValueError()
        if {k:scope[k] for k in ('service','environment','region')}!=grant['allowed_scope']: raise ValueError()
        team=grant['team']; source=grant['sources'][name]
        if any(not isinstance(x,str) or not x.strip() or len(x)>128 for x in (team,source)): raise ValueError()
        if not isinstance(correlation,dict) or set(correlation)!={'run_id','invocation_id','query_scope_ref'}: raise ValueError()
        if any(not isinstance(x,str) or not x.strip() or len(x)>128 for x in correlation.values()): raise ValueError()
        fmt='%Y-%m-%d %H:%M:%S'
        zone=ZoneInfo(grant['timezone'])
        def scope_time(raw):
            try:
                return datetime.strptime(raw,fmt)
            except ValueError:
                parsed=datetime.fromisoformat(raw.replace('Z','+00:00'))
                if parsed.tzinfo is None or parsed.utcoffset() is None: raise ValueError()
                return parsed.astimezone(zone).replace(tzinfo=None)
        start=scope_time(scope['start_at']); end=scope_time(scope['end_at'])
        earliest=datetime.strptime(grant['earliest'],fmt); latest=datetime.strptime(grant['latest'],fmt)
        max_span=grant['max_span_seconds']
        if type(max_span) is not int or not 0<max_span<=3600 or not earliest<=start<end<=latest or (end-start).total_seconds()>max_span: raise ValueError()
        # Ambiguous/nonexistent wall times cannot be safely interpreted.
        for value in (start,end):
            if value.replace(tzinfo=zone,fold=0).utcoffset()!=value.replace(tzinfo=zone,fold=1).utcoffset(): raise ValueError()
        result=dict(args)
        result['scope']={'team':team,'environment':scope['environment'],'region':scope['region'],'service':scope['service'],'source_id':source}
        result['context']=dict(correlation)
        if name!='knowledge_search':
            result['start']=start.replace(tzinfo=zone).isoformat();result['end']=end.replace(tzinfo=zone).isoformat()
        return result
    except (KeyError,TypeError,ValueError,ZoneInfoNotFoundError):
        raise PermissionError('MCP_SCOPE_DENIED') from None

def dod_business_identity(bound_args, query_scope=None):
    """Comparable authoritative resource scope; source_id intentionally excluded."""
    from datetime import datetime, timezone
    try:
        resource = bound_args['scope']
        fields = tuple(resource[key] for key in ('team', 'environment', 'region', 'service'))
        if any(not isinstance(value, str) or not value.strip() for value in fields):
            raise ValueError()
        start_raw = bound_args.get('start') or (query_scope or {}).get('start_at')
        end_raw = bound_args.get('end') or (query_scope or {}).get('end_at')
        start = datetime.fromisoformat(start_raw.replace('Z', '+00:00'))
        end = datetime.fromisoformat(end_raw.replace('Z', '+00:00'))
        if start.tzinfo is None or end.tzinfo is None or start.utcoffset() is None or end.utcoffset() is None or end <= start:
            raise ValueError()
        return (*fields, start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat())
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        raise PermissionError('MCP_SCOPE_DENIED') from None


class ScopeMappingChanged(PermissionError):
    """A previously bound multi-source scope no longer matches deployment policy."""


class SchemaChanged(Exception):
    """The server's current tool schema differs from the pinned binding."""

async def _catalog(session):
    response=await session.list_tools()
    tools=list(getattr(response,'tools',response))
    seen=set()
    cursor=getattr(response,'nextCursor',None)
    while cursor:
        if cursor in seen or len(seen)>=100: raise ValueError('MCP pagination invalid')
        seen.add(cursor)
        response=await session.list_tools(cursor=cursor)
        tools.extend(response.tools)
        cursor=response.nextCursor
    names=tuple(t['name'] if isinstance(t,dict) else t.name for t in tools)
    if len(names)!=len(set(names)): raise ValueError('MCP duplicate tool name')
    return ToolCatalog(names,tuple(tools))

class MCPAdapter:
    def __init__(self,session_factory:Callable,policy:ConnectionPolicy):
        self.session_factory,self.policy=session_factory,policy
    async def discover(self,connection_ref,credential_scope,*,team_id,agent_id):
        self.policy.require(team_id,agent_id,connection_ref,credential_scope)
        async with self.session_factory(connection_ref,credential_scope) as session:
            return await _catalog(session)
    def prepare(
        self,
        connection_ref,
        credential_scope,
        tool_name,
        args,
        *,
        team_id,
        agent_id,
        query_scope,
        correlation=None,
        argument_mapping=None,
        full_schema=None,
    ):
        if argument_mapping is not None:
            if tool_name in BUSINESS_TOOLS:
                raise PermissionError('MCP_MAPPING_CONFLICT')
            from workbench_agent.tools.scope_mapping import bind_arguments, validate_mapping
            matching=[]
            for rule in self.policy._bindings():
                if (rule.team_id,rule.agent_id,rule.server,rule.credential_scope)==(team_id,agent_id,connection_ref,credential_scope) and tool_name in rule.read_tools:
                    for policy in rule.mapping_policies:
                        if isinstance(policy,dict) and policy.get('tool_name')==tool_name:
                            matching.append((rule,policy))
            if len(matching)!=1 or full_schema is None: raise PermissionError('MCP_MAPPING_POLICY_UNAVAILABLE')
            rule,policy=matching[0]
            if (
                policy.get('schema_hash') is not None
                and policy.get('schema_hash')
                != hashlib.sha256(
                    json.dumps(
                        full_schema,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(',', ':'),
                    ).encode('utf-8')
                ).hexdigest()
            ):
                raise PermissionError('MCP_SCHEMA_NOT_APPROVED')
            try:
                validate_mapping(argument_mapping,full_schema,policy)
            except ValueError as error:
                raise PermissionError(str(error)) from None
            grants=[]
            for grant in rule.scope_grants:
                try:
                    grants.append(bind_arguments(argument_mapping,args,query_scope=query_scope,grant=grant,context=correlation or {}))
                except PermissionError: continue
            if len(grants)!=1: raise PermissionError('MCP_SCOPE_MAPPING_UNAVAILABLE')
            return grants[0]
        if tool_name not in BUSINESS_TOOLS: return args
        for rule in self.policy._bindings():
            if (rule.team_id,rule.agent_id,rule.server,rule.credential_scope)==(team_id,agent_id,connection_ref,credential_scope) and tool_name in rule.read_tools:
                for grant in rule.scope_grants:
                    try:
                        if tool_name in DOD_MCP_TOOLS: return build_dod_arguments(tool_name,args,query_scope,grant,correlation)
                        return build_business_arguments(tool_name,args,query_scope,grant)
                    except PermissionError: pass
        raise PermissionError('MCP_SCOPE_MAPPING_UNAVAILABLE')

    async def call(self,connection_ref,credential_scope,tool_name,args,*,team_id,agent_id,expected_schema=None,query_scope=None,correlation=None,expected_business_scope=None,expected_knowledge_scope=None,argument_mapping=None):
        if tool_name in {'SRE-Log-Query','SRE-Log-TraceQuery'} and any(key in args for key in ('session_key','smart_thread_id','smart_user_id','parameter_template_id','instantiated_parameters')):
            raise PermissionError('MCP_UNSAFE_ARGUMENT')
        prepare_options={
            'team_id':team_id,
            'agent_id':agent_id,
            'query_scope':query_scope,
            'correlation':correlation,
        }
        prepare_options.update(
            argument_mapping=argument_mapping,
            full_schema=expected_schema if argument_mapping is not None else None,
        )
        prepared=self.prepare(
            connection_ref,
            credential_scope,
            tool_name,
            args,
            **prepare_options,
        )
        if expected_knowledge_scope is not None and prepared.get('scope') != expected_knowledge_scope:
            raise ScopeMappingChanged('MCP_SCOPE_CHANGED')
        if expected_business_scope is not None and dod_business_identity(prepared, query_scope) != tuple(expected_business_scope):
            raise ScopeMappingChanged('MCP_SCOPE_CHANGED')
        self.policy.require(team_id,agent_id,connection_ref,credential_scope,tool_name)
        mismatch=False
        result=None
        async with self.session_factory(connection_ref,credential_scope) as session:
            if expected_schema is not None:
                catalog=await _catalog(session)
                if tool_name not in catalog.names: mismatch=True
                else:
                    tool=catalog.tools[catalog.names.index(tool_name)]
                    schema=tool.get('inputSchema') if isinstance(tool,dict) else tool.inputSchema
                    mismatch=schema!=expected_schema
            if not mismatch: result=await session.call_tool(tool_name,prepared)
        if mismatch: raise SchemaChanged()
        return result

class StreamableSessionFactory:
    """Endpoints and header factories are deployment inputs, never model arguments."""
    def __init__(self,connections,headers,*,transport=None,loopback_http=()):
        self.connections,self.headers,self.transport=connections,headers,transport
        self.loopback_http=frozenset(loopback_http)
    @asynccontextmanager
    async def __call__(self,server,credential_scope):
        endpoint=self.connections[(server,credential_scope)]
        if not endpoint.startswith('https://') and (server,credential_scope) not in self.loopback_http: raise ValueError('MCP HTTPS required')
        async with httpx.AsyncClient(headers=self.headers(server,credential_scope),follow_redirects=False,trust_env=False,transport=self.transport) as client:
            async with streamable_http_client(endpoint,http_client=client) as (read,write,_):
                async with ClientSession(read,write) as session:
                    await session.initialize()
                    yield session


def configured_adapter(bindings,secrets):
    """Build only from deployment-owned endpoint/reference/scope bindings."""
    if not bindings: return None
    endpoints={}
    references={}
    loopback_http=set()
    rules=[]
    reviewed=BUSINESS_TOOLS
    for b in bindings:
        mapped=set(b.tool_conversation_allowed_tools)
        if set(b.read_tools)-reviewed-mapped: raise ValueError('unreviewed MCP tool')
        key=(b.server,b.credential_scope)
        if key in endpoints and (endpoints[key]!=b.endpoint or references[key]!=b.secret_ref):
            raise ValueError('conflicting MCP deployment connection')
        endpoints[key]=b.endpoint
        references[key]=b.secret_ref
        if b.endpoint.startswith('http://') and b.allow_loopback_http: loopback_http.add(key)
        rules.append(TrustedConnection(
            b.team_id,
            b.agent_id,
            b.server,
            b.credential_scope,
            frozenset(b.read_tools),
            tuple(b.scope_grants),
            [value.model_dump(mode='json') for value in b.mapping_policies],
            frozenset(b.tool_conversation_allowed_tools),
        ))
    def headers(server,scope):
        return {'Authorization':'Bearer '+secrets.resolve(references[(server,scope)])}
    return MCPAdapter(StreamableSessionFactory(endpoints,headers,loopback_http=loopback_http),ConnectionPolicy(rules))
