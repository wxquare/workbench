import type { ApiClient } from './api'
import type { ModelRef } from './agents-api'

export interface ToolConnection {
  server: string
  credential_scope: string
  read_tools: string[]
  status: string
}
export interface ToolBinding {
  alias: string
  provider: string
  tool_name: string
  server: string
  credential_scope: string
  permission: string
  input_schema: Record<string, unknown>
  output_schema?: Record<string, unknown> | null
  timeout_seconds?: number
  max_output_bytes?: number
  read_only?: boolean
}
export interface ToolSummary {
  alias: string
  draft_revision: number | null
  latest_ref: ModelRef | null
}
interface Client {
  request<T>(path: string, init?: RequestInit): Promise<T>
}

export class ToolApi {
  private keys = new Map<string, { signature: string; key: string }>()
  constructor(private client: Client, private makeKey = () => crypto.randomUUID()) {}
  connections(agent: string) {
    return this.client.request<{items: ToolConnection[]; next_cursor: null}>(
      `/v1/agents/${encodeURIComponent(agent)}/tool-connections`,
    )
  }
  bindings(agent: string, after?: string | null) {
    const query = new URLSearchParams({limit: '100'})
    if (after) query.set('after', after)
    return this.client.request<{items: ToolSummary[]; next_cursor: string | null}>(
      `/v1/agents/${encodeURIComponent(agent)}/tool-bindings?${query}`,
    )
  }
  binding(agent: string, alias: string) {
    return this.client.request(`/v1/agents/${encodeURIComponent(agent)}/tool-bindings/${encodeURIComponent(alias)}`)
  }
  discover(agent: string, server: string, scope: string) {
    const operation = `discover:${agent}:${server}:${scope}`
    return this.mutate(operation, `/v1/agents/${encodeURIComponent(agent)}/tool-connections/${encodeURIComponent(server)}/discover`,
      'POST', undefined, {credential_scope: scope})
  }
  save(agent: string, binding: ToolBinding, revision: number) {
    return this.mutate(`save:${agent}:${binding.alias}`, `/v1/agents/${encodeURIComponent(agent)}/tool-bindings/${encodeURIComponent(binding.alias)}`,
      'PUT', revision, binding)
  }
  publish(agent: string, alias: string, revision: number) {
    return this.mutate(`publish:${agent}:${alias}`, `/v1/agents/${encodeURIComponent(agent)}/tool-bindings/${encodeURIComponent(alias)}/publish`,
      'POST', revision)
  }
  probe(agent: string, alias: string) {
    return this.mutate(`probe:${agent}:${alias}`, `/v1/agents/${encodeURIComponent(agent)}/tool-bindings/${encodeURIComponent(alias)}/probe`,
      'POST')
  }
  job(agent: string, id: string) {
    return this.client.request<{job_id: string; status: string; result_ref?: string | null}>(
      `/v1/agents/${encodeURIComponent(agent)}/admin-jobs/${encodeURIComponent(id)}`,
    )
  }
  private async mutate<T>(operation: string, path: string, method: string, revision?: number, body?: unknown): Promise<T> {
    const signature = JSON.stringify({path, method, revision, body})
    let pending = this.keys.get(operation)
    if (!pending || pending.signature !== signature) {
      pending = {signature, key: this.makeKey()}
      this.keys.set(operation, pending)
    }
    const headers: Record<string, string> = {'Idempotency-Key': pending.key}
    if (revision !== undefined) headers['If-Match'] = String(revision)
    if (body !== undefined) headers['Content-Type'] = 'application/json'
    const result = await this.client.request<T>(path, {
      method,
      headers,
      ...(body === undefined ? {} : {body: JSON.stringify(body)}),
    })
    if (this.keys.get(operation) === pending) this.keys.delete(operation)
    return result
  }
}
