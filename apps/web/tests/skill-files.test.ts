import { describe, expect, it } from 'vitest'
import { filesError, imageMime, normalizeFiles, pathError, readLocalFile } from '../src/skill-files'

function local(name: string, bytes: Uint8Array): File {
  return { name, size: bytes.length, arrayBuffer: async () => bytes.buffer } as File
}

describe('skill resource encoding', () => {
  it('keeps old main-only drafts and decodes local scripts as text', async () => {
    expect(normalizeFiles({ content: '指令' })).toEqual({ 'SKILL.md': { encoding: 'utf-8', content: '指令' } })
    expect(await readLocalFile(local('a.py', new TextEncoder().encode('print("证据")')), 'scripts/a.py')).toEqual({ encoding: 'utf-8', content: 'print("证据")' })
  })
  it('rejects an image extension whose bytes are actually script text', async () => {
    await expect(readLocalFile(local('fake.png', new TextEncoder().encode('<svg onload="alert(1)"/>')), 'assets/fake.png')).rejects.toThrow('图片')
  })
  it('blocks path escapes, directory collisions and UTF-8 byte overflow', () => {
    const files = { 'SKILL.md': { encoding: 'utf-8' as const, content: 'main' }, 'references/a.md': { encoding: 'utf-8' as const, content: 'a' } }
    expect(pathError('../a', files)).not.toBe('')
    expect(pathError('references', files)).not.toBe('')
    expect(filesError({ 'SKILL.md': { encoding: 'utf-8', content: '文'.repeat(10667) } })).toContain('大小限制')
  })
  it('only derives previews for known raster extensions', () => {
    expect(imageMime('assets/a.constructor')).toBeUndefined()
    expect(imageMime('assets/a.svg')).toBeUndefined()
    expect(imageMime('assets/a.PNG')).toBe('image/png')
  })
})
