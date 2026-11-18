import type { KnowledgeCitation } from './api'

interface Match { source_id: string; title: string; content: string; source_locator?: string | null }
export interface EvidenceContent { evidence_id: string; content: { schema_version?: string; status?: string; data?: { matches?: Match[] } } }
export interface VisibleCitation extends KnowledgeCitation { title: string; source_locator: string | null }

/** Validate against the currently readable evidence, not metadata supplied by the answer. */
export function verifiedCitations(citations: KnowledgeCitation[], evidence: Record<string, EvidenceContent>): VisibleCitation[] | null {
  const result: VisibleCitation[] = []
  for (const citation of citations) {
    const source = evidence[citation.evidence_ref]
    if (!source || source.evidence_id !== citation.evidence_ref || source.content?.schema_version !== 'dod-tool-result-v1' || source.content.status !== 'ok') return null
    const matches = source.content.data?.matches
    if (!Array.isArray(matches)) return null
    const hit = matches.find(item => item.source_id === citation.source_id && typeof item.content === 'string' && citation.quote.trim() && item.content.includes(citation.quote))
    if (!hit || typeof hit.title !== 'string') return null
    result.push({ ...citation, title: hit.title, source_locator: hit.source_locator ?? null, source_uri: null })
  }
  return result
}

export function knowledgeRoute(source: string) {
  const match = /^kb:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})@([1-9]\d*)#([0-9a-f]{24})$/i.exec(source)
  return match ? { id: match[1], version: match[2], hash: `#${match[3]}` } : null
}
