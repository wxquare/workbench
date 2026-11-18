export type KnowledgeKind = 'faq' | 'runbook' | 'glossary' | 'case'
export interface KBDraft {
  title: string; kind: KnowledgeKind; body_md: string
  service?: string | null; environment?: string | null; region?: string | null
  tags?: string[]; aliases?: string[]; review_by?: string | null
}
export interface EntryView {
  entry_id: string; title: string; kind: KnowledgeKind; status: 'draft' | 'published' | 'archived'
  revision: number; published_version: number | null; has_unpublished_changes: boolean
  owner: string; created_at: number; updated_at: number; draft?: KBDraft
  service: string | null; environment: string | null; region: string | null
  tags?: string[]; aliases?: string[]
}
export interface KBChunk { chunk_id: string; heading_path: string[]; line_start: number; line_end: number; content: string; content_hash: string; chunker_version: string }
export interface PublishedVersion { entry_id: string; version: number; content_hash: string; created_at: number; author?: string; change_note?: string | null }
export interface VersionView extends KBDraft, PublishedVersion { status: string; author: string; chunks: KBChunk[] }
export interface SearchHit { entry_id: string; version: number; chunk: KBChunk; title: string; kind: KnowledgeKind; updated_at: number; source_id: string }
export interface SearchScope { service: string; environment: string; region: string }
export interface SearchPreview { matches: SearchHit[]; warnings: string[] }
export interface KnowledgeGap {
  gap_id: string; run_id: string; problem_type: 'kb.empty' | 'kb.unanswerable' | 'kb.invalid_citation'
  review_status: 'open' | 'resolved' | 'dismissed'; summary: string; gap_revision: number
  query_scope: SearchScope | null; resolved_entry_id: string | null; resolved_version: number | null
  replay_run_id: string | null; verification_status: string | null; note: string | null; created_at: number; updated_at: number
}
export interface ReplayAccepted { run_id: string; thread_id: string; status: string; links: Record<string, string> }
interface Page<T, C = string> { items: T[]; next_cursor: C | null }
interface Client { request<T>(path: string, init?: RequestInit): Promise<T> }

export class KnowledgeApi {
  private pending = new Map<string, { signature: string; key: string }>()
  constructor(private client: Client, private makeKey = () => crypto.randomUUID()) {}
  reset() { this.pending.clear() }
  list(filters: Record<string, string> = {}, after?: string) {
    const query = new URLSearchParams({ limit: '100', ...filters })
    if (after) query.set('after', after)
    return this.client.request<Page<EntryView>>(`/v1/kb/entries?${query}`)
  }
  async allEntries(filters: Record<string, string> = {}) {
    const items: EntryView[] = [], seen = new Set<string>()
    let cursor: string | undefined
    do {
      const page = await this.list(filters, cursor)
      if (!Array.isArray(page.items)) throw new Error('知识条目列表格式无效。')
      items.push(...page.items); cursor = page.next_cursor || undefined
      if (cursor && seen.has(cursor)) throw new Error('知识条目分页游标重复。')
      if (cursor) seen.add(cursor)
    } while (cursor)
    return items
  }
  get(id: string) { return this.client.request<EntryView>(`/v1/kb/entries/${encodeURIComponent(id)}`) }
  versions(id: string, after?: number) {
    const query = after === undefined ? '' : `?after=${after}`
    return this.client.request<Page<PublishedVersion, number>>(`/v1/kb/entries/${encodeURIComponent(id)}/versions${query}`)
  }
  async allVersions(id: string) {
    const items: PublishedVersion[] = [], seen = new Set<number>()
    let cursor: number | undefined
    do {
      const page = await this.versions(id, cursor)
      items.push(...page.items); cursor = page.next_cursor ?? undefined
      if (cursor !== undefined && seen.has(cursor)) throw new Error('知识版本分页游标重复。')
      if (cursor !== undefined) seen.add(cursor)
    } while (cursor !== undefined)
    return items
  }
  version(id: string, version: number) { return this.client.request<VersionView>(`/v1/kb/entries/${encodeURIComponent(id)}/versions/${version}`) }
  search(query: string, scope: SearchScope, limit = 3) {
    return this.client.request<SearchPreview>('/v1/kb/search', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ query, scope, limit }) })
  }
  gaps(status: 'open' | 'resolved' | 'dismissed' = 'open', after?: string) {
    const query = new URLSearchParams({ status, limit: '100' })
    if (after) query.set('after', after)
    return this.client.request<Page<KnowledgeGap>>(`/v1/kb/gaps?${query}`)
  }
  getGap(id: string) { return this.client.request<KnowledgeGap>(`/v1/kb/gaps/${encodeURIComponent(id)}`) }
  toDraft(id: string, revision: number) { return this.mutate<EntryView>('gap-draft', `/v1/kb/gaps/${encodeURIComponent(id)}/draft`, 'POST', revision) }
  replayGap(id: string, revision: number) { return this.mutate<ReplayAccepted>('gap-replay', `/v1/kb/gaps/${encodeURIComponent(id)}/replay`, 'POST', revision) }
  updateGap(id: string, revision: number, body: { status: 'resolved' | 'dismissed'; note: string; entry_id?: string; version?: number; verification_run_id?: string }) {
    return this.mutate<KnowledgeGap>('gap-update', `/v1/kb/gaps/${encodeURIComponent(id)}`, 'PATCH', revision, body)
  }
  private async mutate<T>(operation: string, path: string, method: string, revision?: number, body?: unknown, raw?: { bytes: Uint8Array; filename: string; contentType: string }) {
    const signature = JSON.stringify({ path, method, revision, body, raw: raw && { filename: raw.filename, bytes: Array.from(raw.bytes) } })
    let pending = this.pending.get(operation)
    if (!pending || pending.signature !== signature) { pending = { signature, key: this.makeKey() }; this.pending.set(operation, pending) }
    const headers: Record<string, string> = { 'Idempotency-Key': pending.key }
    if (revision !== undefined) headers['If-Match'] = String(revision)
    if (body !== undefined) headers['Content-Type'] = 'application/json'
    if (raw) { headers['Content-Type'] = raw.contentType; headers['X-Filename'] = encodeURIComponent(raw.filename) }
    const payload = raw ? new Blob([raw.bytes as BlobPart]) : body === undefined ? undefined : JSON.stringify(body)
    const result = await this.client.request<T>(path, { method, headers, ...(payload === undefined ? {} : { body: payload }) })
    if (this.pending.get(operation) === pending) this.pending.delete(operation)
    return result
  }
  create(draft: KBDraft) { return this.mutate<EntryView>('create', '/v1/kb/entries', 'POST', undefined, draft) }
  save(id: string, revision: number, draft: KBDraft) { return this.mutate<EntryView>('save', `/v1/kb/entries/${encodeURIComponent(id)}`, 'PATCH', revision, draft) }
  publish(id: string, revision: number) { return this.mutate<PublishedVersion>('publish', `/v1/kb/entries/${encodeURIComponent(id)}/publish`, 'POST', revision) }
  archive(id: string, revision: number) { return this.mutate<EntryView>('archive', `/v1/kb/entries/${encodeURIComponent(id)}/archive`, 'POST', revision) }
  rollback(id: string, version: number, revision: number) { return this.mutate<PublishedVersion>('rollback', `/v1/kb/entries/${encodeURIComponent(id)}/versions/${version}/rollback`, 'POST', revision) }
  importText(id: string, revision: number, bytes: Uint8Array, filename: string) {
    return this.mutate<EntryView>('import', `/v1/kb/entries/${encodeURIComponent(id)}/import`, 'POST', revision, undefined,
      { bytes, filename, contentType: filename.toLowerCase().endsWith('.txt') ? 'text/plain' : 'text/markdown' })
  }
}
