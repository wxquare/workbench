"""Worker-only, fenced MCP metadata discovery; no business query guessed."""

import asyncio
import json
from workbench_agent.application.authorization import Authorization
from workbench_agent.domain.identity import AuthContext
from workbench_agent.domain.tools import ToolBinding
from workbench_agent.jobs.admin import AdminJobGuard


class ToolProbe:
    def __init__(self, backend, adapter, *, probe_requests=None):
        self.backend, self.adapter, self.probe_requests = backend, adapter, probe_requests or {}

    async def _check(self, claim, auth):
        async with self.backend.create_uow().begin_write() as uow:
            await AdminJobGuard(self.backend.admin_jobs).validate(uow, claim)
            await Authorization(self.backend).current(uow, auth, claim.agent_id, 'admin')

    async def execute(self, claim):
        payload = json.loads(claim.payload)
        auth = AuthContext(
            team_id=claim.team_id,
            subject=payload['subject'],
            identity_kind=payload['identity_kind'],
            agent_scope=payload['agent_scope'],
            roles=(),
        )
        binding = ToolBinding.model_validate(payload['binding'])
        await self._check(claim, auth)
        report = {
            'configuration': 'valid',
            'discovery': 'not_run',
            'schema': 'unknown',
            'invocation': 'not_run',
            'revision': payload['revision'],
        }
        if self.adapter is None:
            report['configuration'] = 'unavailable'
        else:
            try:
                catalog = await asyncio.wait_for(
                    self.adapter.discover(
                        binding.server,
                        binding.credential_scope,
                        team_id=claim.team_id,
                        agent_id=claim.agent_id,
                    ),
                    binding.timeout_seconds,
                )
                report['discovery'] = 'ok'
                if binding.tool_name not in catalog.names:
                    report['schema'] = 'missing'
                else:
                    tool = catalog.tools[catalog.names.index(binding.tool_name)]
                    schema = tool.get('inputSchema') if isinstance(tool, dict) else tool.inputSchema
                    report['schema'] = 'match' if schema == binding.input_schema else 'mismatch'
            except PermissionError:
                report['discovery'] = 'denied'
            except TimeoutError:
                report['discovery'] = 'timeout'
            except Exception:
                report['discovery'] = 'unavailable'
        request = self.probe_requests.get(
            (
                claim.team_id,
                claim.agent_id,
                binding.server,
                binding.credential_scope,
                binding.tool_name,
            )
        )
        if request is not None and binding.read_only and report['schema'] == 'match':
            from jsonschema import Draft202012Validator

            forbidden = {
                'session_key',
                'smart_thread_id',
                'smart_user_id',
                'parameter_template_id',
                'instantiated_parameters',
            }
            from workbench_agent.tools.mcp import BUSINESS_TOOLS, DOD_MCP_TOOLS

            query_scope = None
            try:
                if binding.tool_name in BUSINESS_TOOLS:
                    if not isinstance(request, dict) or set(request) != {'scope', 'args'}:
                        raise PermissionError()
                    query_scope = request['scope']
                    request = request['args']
                    correlation = {
                        'run_id': 'probe-' + claim.job_id,
                        'invocation_id': 'probe-' + claim.job_id,
                        'query_scope_ref': 'probe-' + claim.job_id,
                    }
                    options = (
                        {'correlation': correlation} if binding.tool_name in DOD_MCP_TOOLS else {}
                    )
                    checked = self.adapter.prepare(
                        binding.server,
                        binding.credential_scope,
                        binding.tool_name,
                        request,
                        team_id=claim.team_id,
                        agent_id=claim.agent_id,
                        query_scope=query_scope,
                        **options,
                    )
                else:
                    checked = request
                    options = {}
                if (
                    not isinstance(request, dict)
                    or forbidden.intersection(request)
                    or not Draft202012Validator(binding.input_schema).is_valid(checked)
                ):
                    raise PermissionError()
            except (PermissionError, TypeError, ValueError):
                report['invocation'] = 'denied'
            else:
                await self._check(claim, auth)
                try:
                    result = await asyncio.wait_for(
                        self.adapter.call(
                            binding.server,
                            binding.credential_scope,
                            binding.tool_name,
                            request,
                            team_id=claim.team_id,
                            agent_id=claim.agent_id,
                            expected_schema=binding.input_schema,
                            query_scope=query_scope,
                            **options,
                        ),
                        binding.timeout_seconds,
                    )
                    report['invocation'] = 'failed' if getattr(result, 'isError', False) else 'ok'
                except PermissionError:
                    report['invocation'] = 'denied'
                except TimeoutError:
                    report['invocation'] = 'timeout'
                except Exception:
                    report['invocation'] = 'unavailable'
        await self._check(claim, auth)
        return json.dumps(report, sort_keys=True, separators=(',', ':'))
