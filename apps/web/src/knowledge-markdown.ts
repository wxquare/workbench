export interface MarkdownBlock { kind: 'h1' | 'h2' | 'h3' | 'paragraph' | 'code'; text: string }

export function draftBytes(body: string): number {
  return new TextEncoder().encode(body.replace(/\r\n?/g, '\n')).byteLength
}

/** Renderable text blocks only: never create HTML nodes, links, or image requests from source. */
export function markdownBlocks(body: string): MarkdownBlock[] {
  const result: MarkdownBlock[] = []
  let fence: string | null = null, code: string[] = []
  for (const line of body.replace(/\r\n?/g, '\n').split('\n')) {
    const marker = line.match(/^\s*(`{3,}|~{3,})/)
    if (marker && !fence) { fence = marker[1]; code = []; continue }
    if (marker && fence && marker[1][0] === fence[0] && marker[1].length >= fence.length && !line.slice(marker[0].length).trim()) {
      result.push({ kind: 'code', text: code.join('\n') }); fence = null; continue
    }
    if (fence) { code.push(line); continue }
    const heading = line.match(/^\s*(#{1,3})\s+(.+)$/)
    if (heading) result.push({ kind: `h${heading[1].length}` as 'h1' | 'h2' | 'h3', text: heading[2] })
    else if (line.trim()) result.push({ kind: 'paragraph', text: line })
  }
  if (fence) result.push({ kind: 'code', text: code.join('\n') })
  return result
}
