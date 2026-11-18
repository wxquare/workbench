import { expect, it, vi } from 'vitest'
import { KnowledgeApi } from '../src/knowledge-api'

it('retries the same draft with the same revision and idempotency key', async () => {
  const request = vi.fn().mockRejectedValueOnce(new Error('network')).mockResolvedValueOnce({ entry_id: 'entry', revision: 3 })
  const service = new KnowledgeApi({ request }, () => 'kb-save-key')
  const draft = { title: '库存', kind: 'faq' as const, body_md: '检查库存' }
  await expect(service.save('entry', 2, draft)).rejects.toThrow('network')
  await service.save('entry', 2, draft)
  expect(request.mock.calls[1][0]).toBe('/v1/kb/entries/entry')
  expect(request.mock.calls[1][1].headers).toMatchObject({ 'If-Match': '2', 'Idempotency-Key': 'kb-save-key' })
})

it('rejects repeated pagination cursors instead of looping forever', async () => {
  const request = vi.fn().mockResolvedValue({ items: [], next_cursor: 'same' })
  const service = new KnowledgeApi({ request })
  await expect(service.allEntries()).rejects.toThrow('分页游标重复')
  expect(request).toHaveBeenCalledTimes(2)
})

it('retries gap replay with the original idempotency key', async () => {
  const request = vi.fn().mockRejectedValueOnce(new Error('network')).mockResolvedValueOnce({ run_id: 'new-run', status: 'queued' })
  const service = new KnowledgeApi({ request }, () => 'replay-key')
  await expect(service.replayGap('gap', 2)).rejects.toThrow('network')
  await service.replayGap('gap', 2)
  expect(request.mock.calls[1][0]).toBe('/v1/kb/gaps/gap/replay')
  expect(request.mock.calls[1][1].headers).toMatchObject({ 'If-Match': '2', 'Idempotency-Key': 'replay-key' })
})

it('loads all historical versions using their cursors', async () => {
  const request = vi.fn().mockResolvedValueOnce({ items: [{ version: 51 }], next_cursor: 51 })
    .mockResolvedValueOnce({ items: [{ version: 1 }], next_cursor: null })
  const service = new KnowledgeApi({ request })
  expect((await service.allVersions('entry')).map(v => v.version)).toEqual([51, 1])
  expect(request.mock.calls[1][0]).toContain('after=51')
})

it('encodes a Unicode import filename and retains the idempotency key on retry', async () => {
  const request = vi.fn().mockRejectedValueOnce(new Error('network')).mockResolvedValueOnce({ entry_id: 'entry', revision: 3 })
  const service = new KnowledgeApi({ request }, () => 'import-key')
  const bytes = new TextEncoder().encode('内容')
  await expect(service.importText('entry', 2, bytes, '知识.md')).rejects.toThrow('network')
  await service.importText('entry', 2, bytes, '知识.md')
  expect(request.mock.calls[0][1].headers).toMatchObject({ 'X-Filename': '%E7%9F%A5%E8%AF%86.md', 'Idempotency-Key': 'import-key' })
  expect(request.mock.calls[1][1].headers).toMatchObject({ 'X-Filename': '%E7%9F%A5%E8%AF%86.md', 'Idempotency-Key': 'import-key' })
})
