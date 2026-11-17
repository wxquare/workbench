import type { ModelRef } from './agents-api'

export interface SkillVersion extends ModelRef { created_at?: number }
export interface SkillDraft { description: string; content: string }
export interface Skill {
  skill_id: string; name: string; description: string; revision: number; updated_at: number
  latest_version: SkillVersion | null; versions: SkillVersion[]; draft?: SkillDraft | null
}
interface Client { request<T>(path: string, init?: RequestInit): Promise<T> }

export class SkillApi {
  private pending = new Map<string, { signature: string; key: string }>()
  constructor(private client: Client, private makeKey = () => crypto.randomUUID()) {}
  async list() {
    const items: Skill[] = [], seen = new Set<string>()
    let cursor: string | null = null
    do {
      const query = new URLSearchParams({ limit: '100' })
      if (cursor) query.set('after', cursor)
      const page: { items: Skill[]; next_cursor: string | null } = await this.client.request(`/v1/skills?${query}`)
      if (!Array.isArray(page.items)) throw new Error('技能列表格式无效。')
      items.push(...page.items); cursor = page.next_cursor
      if (cursor && seen.has(cursor)) throw new Error('技能分页游标重复。')
      if (cursor) seen.add(cursor)
    } while (cursor)
    return items
  }
  get(id: string) { return this.client.request<Skill>(`/v1/skills/${encodeURIComponent(id)}`) }
  version(id: string, version: string) { return this.client.request<{ content: string }>(`/v1/skills/${encodeURIComponent(id)}/versions/${encodeURIComponent(version)}`) }
  private async mutate<T>(operation: string, path: string, method: string, revision?: number, body?: unknown) {
    const signature = JSON.stringify({ path, method, revision, body })
    let pending = this.pending.get(operation)
    if (!pending || pending.signature !== signature) { pending = { signature, key: this.makeKey() }; this.pending.set(operation, pending) }
    const headers: Record<string, string> = { 'Idempotency-Key': pending.key }
    if (revision !== undefined) headers['If-Match'] = String(revision)
    if (body !== undefined) headers['Content-Type'] = 'application/json'
    const result = await this.client.request<T>(path, { method, headers, ...(body === undefined ? {} : { body: JSON.stringify(body) }) })
    if (this.pending.get(operation) === pending) this.pending.delete(operation)
    return result
  }
  create(name: string, draft: SkillDraft) { return this.mutate<Skill>('create', '/v1/skills', 'POST', undefined, { name, ...draft }) }
  save(id: string, revision: number, draft: SkillDraft) { return this.mutate<Skill>('save', `/v1/skills/${encodeURIComponent(id)}`, 'PATCH', revision, draft) }
  publish(id: string, revision: number) { return this.mutate<SkillVersion>('publish', `/v1/skills/${encodeURIComponent(id)}/publish`, 'POST', revision) }
}
