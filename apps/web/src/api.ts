export interface Agent { agent_id: string; name: string }
export function agentName(id: string, agents: Agent[]): string {
  return agents.find(item => item.agent_id === id)?.name ?? id
}
export interface QueryScope { service: string; environment: string; region: string; start_at: string; end_at: string }
export interface QaRequest { agent_id: string; text: string; scope?: QueryScope }
export type QaMode = 'llm' | 'knowledge'
export interface KnowledgeCitation { evidence_ref: string; source_id: string; quote: string; title: string; source_locator: string | null; source_uri: string | null }
export interface KnowledgeAnswer { schema_version: 'knowledge-answer-v1'; review_kind: 'model_reviewed'; verification: 'passed' | 'failed'; claims: { text: string; citations: KnowledgeCitation[] }[] }
export interface RunOutcome { summary: string; completed?: boolean; unresolved_items?: string[]; knowledge_gaps?: string[]; findings?: { summary: string }[]; evidence_refs?: string[]; knowledge_answer?: KnowledgeAnswer | null }
export interface WaitingView { name: string; kind: string; actor?: string; interrupt_id: string; revision: number; expires_at_ms: number; created_at_ms: number; schema: Record<string, unknown>; approval_id?: string; args_hash?: string; expected_scope_id?: string }
export type ResumeResponse = { kind: "user_input"; values: Record<string, string> } | { kind: "approval"; approval_id: string; decision: "approve" | "reject"; expected_args_hash: string }
export interface ResumeBody { interrupt_id: string; expected_revision: number; response: ResumeResponse }
export interface RunView { run_id: string; agent_id: string; thread_id: string; status: string; phase: string | null; created_at: string; outcome: RunOutcome | null; revision: number; waiting: WaitingView | null; links?: Record<string, string> }
export interface AcceptedRun { run_id: string }

export class ApiError extends Error {
  constructor(public readonly status: number, message: string, public readonly code?: string) { super(message) }
}

export class ApiClient {
  constructor(private token: () => string, private fetcher: typeof fetch = (...args) => fetch(...args)) {}
  async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const token = this.token().trim()
    if (!token) throw new ApiError(401, '请先提供 Bearer Token。')
    const headers = new Headers(init.headers)
    headers.set('Authorization', `Bearer ${token}`)
    headers.set('Accept', 'application/json')
    const response = await this.fetcher(path, { ...init, headers, credentials: 'same-origin' })
    if (!response.ok) {
      let message = `请求失败 (HTTP ${response.status})`
      let errorCode: string | undefined
      try {
        const body = await response.json()
        const code = typeof body.error === 'string' ? body.error : body.error?.code ?? body.code ?? body.detail
        if (typeof code === 'string') { errorCode = code; message += `: ${code}` }
      } catch { /* HTTP status is still useful. */ }
      throw new ApiError(response.status, message, errorCode)
    }
    return response.json() as Promise<T>
  }
  async agents(): Promise<Agent[]> {
    const items: Agent[] = []
    const seen = new Set<string>()
    let cursor: string | null = null
    do {
      const query = new URLSearchParams({ limit: '100' })
      if (cursor) query.set('after', cursor)
      const page: { items: Agent[]; next_cursor: string | null } = await this.request(`/v1/agents?${query}`)
      items.push(...page.items)
      cursor = page.next_cursor
      if (cursor && seen.has(cursor)) throw new Error('Agent 分页游标重复。')
      if (cursor) seen.add(cursor)
    } while (cursor)
    return items
  }
  createQa(request: QaRequest, key: string): Promise<AcceptedRun> {
    return this.request('/v1/runs', { method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key },
      body: JSON.stringify({ agent_id: request.agent_id, mode: 'qa', input: { text: request.text }, business_context: request.scope ? { query_scope: request.scope } : {} }) })
  }
  cancelRun(id: string, key: string): Promise<RunView> {
    return this.request(`/v1/runs/${encodeURIComponent(id)}/cancel`, { method: 'POST', headers: { 'Idempotency-Key': key } })
  }
  resumeRun(id: string, body: ResumeBody, key: string): Promise<unknown> {
    return this.request(`/v1/runs/${encodeURIComponent(id)}/resume`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key }, body: JSON.stringify(body) })
  }
  run(id: string): Promise<RunView> { return this.request(`/v1/runs/${encodeURIComponent(id)}`) }
}

export function createQaSubmission(client: ApiClient, makeKey = () => crypto.randomUUID()) {
  let previous = '', key = ''
  return async (request: QaRequest): Promise<AcceptedRun> => {
    const signature = JSON.stringify(request)
    if (signature !== previous) { previous = signature; key = makeKey() }
    const accepted = await client.createQa(request, key)
    previous = ''; key = ''
    return accepted
  }
}
export const isRunActive = (status: string) => ['queued', 'running', 'finalizing', 'cancelling', 'waiting_input', 'waiting_approval'].includes(status)
export function missingQaFields(fields: { agent: string; question: string; service: string; region: string; start: string; end: string }, mode: QaMode = 'knowledge'): string[] {
  const labels = { agent: 'Agent', question: '问题', service: '服务', region: '区域', start: '开始时间', end: '结束时间' }
  const keys: (keyof typeof labels)[] = mode === 'llm' ? ['agent', 'question'] : ['agent', 'question', 'service', 'region', 'start', 'end']
  return keys.filter(key => !fields[key].trim()).map(key => labels[key])
}
export function validQaWindow(start: Date, end: Date): boolean {
  const duration = end.getTime() - start.getTime()
  return Number.isFinite(duration) && duration > 0 && duration <= 3600000
}
