import { expect, it } from 'vitest'
import { markdownBlocks, draftBytes } from '../src/knowledge-markdown'

it('preserves untrusted HTML as text rather than interpreting it', () => {
  const blocks = markdownBlocks('# 库存\n<script>alert(1)</script>\n![x](https://example.com/x)')
  expect(blocks).toEqual([
    { kind: 'h1', text: '库存' },
    { kind: 'paragraph', text: '<script>alert(1)</script>' },
    { kind: 'paragraph', text: '![x](https://example.com/x)' },
  ])
})

it('counts UTF-8 bytes and does not parse headings in fenced code', () => {
  expect(draftBytes('中'.repeat(6000))).toBe(18000)
  expect(markdownBlocks('```md\n# literal\n```')).toEqual([{ kind: 'code', text: '# literal' }])
})
