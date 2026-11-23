import type { SkillFile, SkillFiles } from './skills-api'

export const EMPTY_FILES = (): SkillFiles => ({ 'SKILL.md': { encoding: 'utf-8', content: '' } })
const IMAGE_TYPES: Record<string, string> = { png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', gif: 'image/gif', webp: 'image/webp' }
export function imageMime(path: string): string | undefined {
  const extension = path.split('.').pop()?.toLowerCase() ?? ''
  return Object.prototype.hasOwnProperty.call(IMAGE_TYPES, extension) ? IMAGE_TYPES[extension] : undefined
}

export function normalizeFiles(value?: { files?: SkillFiles; content?: string } | null): SkillFiles {
  const files = value?.files ?? { 'SKILL.md': { encoding: 'utf-8', content: value?.content ?? '' } }
  return Object.fromEntries(Object.entries(files).map(([path, file]) => [path, typeof file === 'string' ? { encoding: 'utf-8', content: file } : { ...file }]))
}

export function pathError(path: string, files: SkillFiles, replacing?: string): string {
  const parts = path.split('/')
  if (!path || path.length > 256 || path.includes('\\') || path.includes('\0') || path.startsWith('/') || parts.some(part => !part || part === '.' || part === '..' || part === '.claude')) return '请输入有效的包内相对路径。'
  for (const existing of Object.keys(files)) {
    if (existing === replacing) continue
    if (existing === path) return '该文件路径已存在。'
    if (existing.startsWith(path + '/') || path.startsWith(existing + '/')) return '文件路径与目录冲突。'
  }
  if (!replacing && Object.keys(files).length >= 64) return '最多可添加 64 个文件。'
  return ''
}

export function fileBytes(file: SkillFile): number {
  return file.encoding === 'base64' ? Math.floor(file.content.length * 3 / 4) - (file.content.endsWith('==') ? 2 : file.content.endsWith('=') ? 1 : 0) : new TextEncoder().encode(file.content).length
}

export function filesError(files: SkillFiles): string {
  const main = files['SKILL.md']
  if (!main || main.encoding !== 'utf-8' || !main.content.trim()) return '请填写 SKILL.md 的技能指令。'
  let total = 0
  for (const [path, file] of Object.entries(files)) {
    const error = pathError(path, files, path)
    if (error) return error
    const size = fileBytes(file); total += size
    if (file.encoding === 'utf-8' && file.content.includes('\0')) return '文本文件不能包含空字符。'
    if (size > (path === 'SKILL.md' ? 32000 : file.encoding === 'base64' ? 2 * 1024 * 1024 : 256 * 1024)) return `文件 ${path} 超过大小限制。`
    if (file.encoding === 'base64' && !imageMime(path)) return '仅支持 PNG、JPEG、GIF、WebP 图片。'
  }
  if (Object.keys(files).length > 64) return '最多可添加 64 个文件。'
  return total > 8 * 1024 * 1024 ? '文件总大小不能超过 8 MiB。' : ''
}

export async function readLocalFile(file: File, path: string): Promise<SkillFile> {
  const mime = imageMime(path)
  if (file.size > (mime ? 2 * 1024 * 1024 : path === 'SKILL.md' ? 32000 : 256 * 1024)) throw new Error('上传文件超过大小限制。')
  const bytes = new Uint8Array(await file.arrayBuffer())
  if (!mime) return { encoding: 'utf-8', content: new TextDecoder('utf-8', { fatal: true }).decode(bytes) }
  const starts = (...prefix: number[]) => prefix.every((value, index) => bytes[index] === value)
  const valid = mime === 'image/png' ? starts(137, 80, 78, 71, 13, 10, 26, 10)
    : mime === 'image/jpeg' ? starts(255, 216, 255)
    : mime === 'image/gif' ? new TextDecoder().decode(bytes.subarray(0, 6)).match(/^GIF8[79]a$/)
    : new TextDecoder().decode(bytes.subarray(0, 4)) === 'RIFF' && new TextDecoder().decode(bytes.subarray(8, 12)) === 'WEBP'
  if (!valid) throw new Error('图片内容与文件类型不匹配。')
  let binary = ''
  for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode(...bytes.subarray(i, i + 8192))
  return { encoding: 'base64', content: btoa(binary) }
}
