import { expect, it } from 'vitest'
import { verifiedCitations, knowledgeRoute } from '../src/knowledge-citations'

it('reveals a citation only when the currently readable evidence contains the exact quote', () => {
  const source = 'kb:11111111-1111-1111-1111-111111111111@2#abcdef123456abcdef123456'
  const citation = { evidence_ref: 'evidence', source_id: source, quote: '检查库存', title: 'untrusted', source_uri: 'https://bad.test', source_locator: null }
  const evidence = { evidence_id: 'evidence', content: { schema_version: 'dod-tool-result-v1', status: 'ok',
    data: { matches: [{ source_id: source, title: '可信版本', content: '先检查库存。', source_locator: '行 2', source_uri: null }] } } }
  const visible = verifiedCitations([citation], { evidence })
  expect(visible?.[0].title).toBe('可信版本')
  expect(knowledgeRoute(source)).toEqual({ id: '11111111-1111-1111-1111-111111111111', version: '2', hash: '#abcdef123456abcdef123456' })
  expect(verifiedCitations([{ ...citation, quote: '删除库存' }], { evidence })).toBeNull()
  expect(verifiedCitations([citation], {})).toBeNull()
  expect(knowledgeRoute('https://bad.test')).toBeNull()
})
