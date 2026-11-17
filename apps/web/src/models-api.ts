import type { ApiClient } from './api'

export interface ModelDraft {
  name: string
  provider: 'deepseek'
  adapter: 'deepseek'
  protocol: 'openai_chat_completions'
  base_url: string
  model_id: string
  secret_ref: string
  temperature: number
  request_timeout_seconds: number
  max_output_tokens: number
  max_context_tokens: number
  streaming: boolean
  sdk_max_retries: 0
  invocation_max_attempts: number
  structured_output_strategy: 'json_mode'
  provider_options: { thinking: { type: 'disabled' } }
}
export interface ModelProfile { profile_id: string; name: string; revision: number; active_version: number | null; draft: ModelDraft }
export interface ProbeJob { job_id: string; status: string; result_ref: string | null }
interface RequestClient { request<T>(path: string, init?: RequestInit): Promise<T> }

export function copyModelDraft(draft: ModelDraft): ModelDraft {
  return JSON.parse(JSON.stringify(draft)) as ModelDraft
}

export function createModelsApi(client: ApiClient, makeKey = () => crypto.randomUUID()) {
  const request = client as unknown as RequestClient
  const pending = new Map<string, string>()
  const root = (agent: string) => `/v1/agents/${encodeURIComponent(agent)}/model-profiles`
  const path = (agent: string, name: string) => `${root(agent)}/${encodeURIComponent(name)}`
  const validateSecret = (data: Partial<ModelDraft>) => {
    if (data.secret_ref !== undefined) {
      try {
        const ref = new URL(data.secret_ref)
        if (!['env:', 'secret:'].includes(ref.protocol) || !ref.hostname || ref.username || ref.password || ref.search || ref.hash) throw new Error()
      } catch { throw new Error('密钥必须使用 env:// 或 secret:// 引用。') }
    }
    if (data.base_url !== undefined) {
      try {
        const endpoint = new URL(data.base_url)
        if (endpoint.protocol !== 'https:' || !endpoint.hostname || endpoint.username || endpoint.password || endpoint.search || endpoint.hash) throw new Error()
      } catch { throw new Error('服务地址必须是不包含凭据和查询参数的 HTTPS 地址。') }
    }
  }
  async function mutate<T>(url: string, method: string, body?: Partial<ModelDraft>, revision?: number): Promise<T> {
    if (body) validateSecret(body)
    const signature = JSON.stringify([url, method, body, revision])
    let key = pending.get(signature)
    if (!key) { key = makeKey(); pending.set(signature, key) }
    const headers: Record<string, string> = { 'Idempotency-Key': key }
    if (body) headers['Content-Type'] = 'application/json'
    if (revision !== undefined) headers['If-Match'] = String(revision)
    const result = await request.request<T>(url, { method, headers, ...(body ? { body: JSON.stringify(body) } : {}) })
    pending.delete(signature)
    return result
  }
  return {
    async list(agent: string): Promise<ModelProfile[]> {
      const items: ModelProfile[] = [], seen = new Set<string>()
      let cursor: string | null = null
      do {
        const query = new URLSearchParams({ limit: '100' })
        if (cursor) query.set('after', cursor)
        const page: { items: ModelProfile[]; next_cursor: string | null } = await request.request(`${root(agent)}?${query}`)
        items.push(...page.items)
        cursor = page.next_cursor
        if (cursor && seen.has(cursor)) throw new Error('模型分页游标重复。')
        if (cursor) seen.add(cursor)
      } while (cursor)
      return items
    },
    get: (agent: string, name: string) => request.request<ModelProfile>(path(agent, name)),
    create: (agent: string, draft: Partial<ModelDraft>) => mutate<ModelProfile>(root(agent), 'POST', draft),
    patch: (agent: string, name: string, draft: Partial<ModelDraft>, revision: number) => mutate<ModelProfile>(path(agent, name), 'PATCH', draft, revision),
    setCredential: (agent: string, name: string, apiKey: string, revision: number) => request.request<ModelProfile>(`${path(agent, name)}/credential`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json', 'If-Match': String(revision) }, body: JSON.stringify({ api_key: apiKey }),
    }),
    publish: (agent: string, name: string, revision: number) => mutate<unknown>(`${path(agent, name)}/publish`, 'POST', undefined, revision),
    probe: (agent: string, name: string) => mutate<ProbeJob>(`${path(agent, name)}/probe`, 'POST'),
    job: (agent: string, id: string) => request.request<ProbeJob>(`/v1/agents/${encodeURIComponent(agent)}/admin-jobs/${encodeURIComponent(id)}`),
  }
}
