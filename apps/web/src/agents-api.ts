import { ApiError, type Agent } from './api'

export type Draft = Record<string, unknown>
export interface ModelRef { resource_id: string; version: string; content_hash: string }
export interface AvailableModel { name: string; active_ref: ModelRef }
export interface AgentDetail extends Agent {
  revision: number
  active_profile_id: string | null
  draft: Draft | null
}
interface RequestClient {
  request<T>(path: string, init?: RequestInit): Promise<T>
  agents(): Promise<Agent[]>
}

function isObject(value: unknown): value is Draft {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function mergeDraft(original: Draft, edits: Draft): Draft {
  const merged = { ...original }
  for (const [key, value] of Object.entries(edits)) {
    merged[key] = isObject(value) && isObject(original[key]) ? mergeDraft(original[key], value) : value
  }
  return merged
}

export function parseAgentDraft(text: string, original: Draft = {}): Draft {
  let parsed: unknown
  try { parsed = JSON.parse(text) } catch { throw new Error('配置不是有效的 JSON。') }
  if (!isObject(parsed)) throw new Error('配置必须是 JSON 对象。')
  const draft = mergeDraft(original, parsed)
  if (!isObject(draft.default_model)) throw new Error('配置必须包含 default_model。')
  return draft
}

export function agentError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return `身份验证失败：${error.message}`
    if (error.status === 403) return `权限不足：${error.message}`
    if (error.status === 409 && error.message.includes('REVISION_CONFLICT')) return '版本冲突：配置已被修改。请复制或保留当前编辑内容，再重新加载。'
  }
  return error instanceof Error ? error.message : '请求失败，请重试。'
}

export class AgentApi {
  private pending = new Map<string, { signature: string; key: string }>()
  constructor(private client: RequestClient, private makeKey = () => crypto.randomUUID()) {}

  list() { return this.client.agents() }
  get(id: string) { return this.client.request<AgentDetail>(`/v1/agents/${encodeURIComponent(id)}`) }
  async models(id: string): Promise<AvailableModel[]> {
    const models: AvailableModel[] = [], seen = new Set<string>()
    let cursor: string | null = null
    do {
      const query = new URLSearchParams({ limit: '100' })
      if (cursor) query.set('after', cursor)
      const page: { items: { name: string; active_ref?: ModelRef | null }[]; next_cursor: string | null } =
        await this.client.request(`/v1/agents/${encodeURIComponent(id)}/model-profiles?${query}`)
      for (const item of page.items) {
        const ref = item.active_ref
        if (ref && typeof ref.resource_id === 'string' && ref.resource_id && typeof ref.version === 'string' && ref.version &&
          typeof ref.content_hash === 'string' && /^[0-9a-f]{64}$/.test(ref.content_hash)) models.push({ name: item.name, active_ref: ref })
      }
      cursor = page.next_cursor
      if (cursor && seen.has(cursor)) throw new Error('模型分页游标重复。')
      if (cursor) seen.add(cursor)
    } while (cursor)
    return models
  }

  private async mutate<T>(operation: string, path: string, method: string, revision?: number, body?: unknown): Promise<T> {
    const signature = JSON.stringify({ path, method, revision, body })
    let pending = this.pending.get(operation)
    if (!pending || pending.signature !== signature) {
      pending = { signature, key: this.makeKey() }
      this.pending.set(operation, pending)
    }
    const headers: Record<string, string> = { 'Idempotency-Key': pending.key }
    if (revision !== undefined) headers['If-Match'] = String(revision)
    if (body !== undefined) headers['Content-Type'] = 'application/json'
    const result = await this.client.request<T>(path, { method, headers, ...(body === undefined ? {} : { body: JSON.stringify(body) }) })
    if (this.pending.get(operation) === pending) this.pending.delete(operation)
    return result
  }

  create(name: string) {
    return this.mutate<Agent & { revision: number }>('create', '/v1/agents', 'POST', undefined, { name })
  }
  save(id: string, revision: number, draft: Draft) {
    return this.mutate<{ agent_id: string; revision: number; draft: Draft }>('save', `/v1/agents/${encodeURIComponent(id)}`, 'PATCH', revision, draft)
  }
  publish(id: string, revision: number) {
    return this.mutate<{ profile_id: string; version: string }>('publish', `/v1/agents/${encodeURIComponent(id)}/profiles/publish`, 'POST', revision)
  }
}
