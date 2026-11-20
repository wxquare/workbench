import * as Vue from 'vue'
import { createRenderer, nextTick, ssrContextKey } from 'vue'
import { compile } from '@vue/compiler-dom'
import { compileScript, parse } from '@vue/compiler-sfc'
import { expect, it, vi } from 'vitest'
import KnowledgeEntry from '../src/views/KnowledgeEntry.vue'
import source from '../src/views/KnowledgeEntry.vue?raw'
import { api, bearerToken } from '../src/session'

const route = Vue.reactive({
  params: { id: '8b0c93dd-9210-4b76-95e4-a94e60f5c123', version: '1' },
  hash: '',
})
vi.mock('vue-router', () => ({
  useRoute: () => route,
  useRouter: () => ({ push: vi.fn() }),
}))
vi.mock('../src/session', async () => {
  const { ref } = await import('vue')
  return { bearerToken: ref('token'), api: { request: vi.fn() } }
})

interface Node {
  type: string
  text: string
  children: Node[]
  parent: Node | null
  props: Record<string, any>
}
function node(type: string, text = ''): Node {
  return { type, text, children: [], parent: null, props: {} }
}
const renderer = createRenderer<Node, Node>({
  createElement: type => node(type),
  createText: text => node('#text', text),
  createComment: text => node('#comment', text),
  setText: (el, text) => { el.text = text },
  setElementText: (el, text) => { el.text = text; el.children = [] },
  insert: (el, parent, anchor) => {
    el.parent = parent
    const index = anchor ? parent.children.indexOf(anchor) : -1
    if (index < 0) parent.children.push(el)
    else parent.children.splice(index, 0, el)
  },
  remove: el => {
    if (el.parent) el.parent.children.splice(el.parent.children.indexOf(el), 1)
  },
  parentNode: el => el.parent,
  nextSibling: el => (
    el.parent?.children[el.parent.children.indexOf(el) + 1] ?? null
  ),
  patchProp: (el, key, _old, value) => { el.props[key] = value },
})
function all(root: Node): Node[] {
  return [root, ...root.children.flatMap(all)]
}
function text(root: Node): string {
  return root.text + root.children.map(text).join('')
}
async function flush() {
  for (let index = 0; index < 8; index++) {
    await Promise.resolve()
    await nextTick()
  }
}
function component() {
  const descriptor = parse(source).descriptor
  const bindings = compileScript(descriptor, { id: 'knowledge-version-test' }).bindings
  const code = compile(
    descriptor.template!.content,
    { mode: 'function', prefixIdentifiers: true, bindingMetadata: bindings },
  ).code
  return { ...KnowledgeEntry, render: new Function('Vue', code)(Vue) }
}
function version(value: number, hash: string) {
  return {
    entry_id: route.params.id,
    title: `Runbook v${value}`,
    kind: 'runbook',
    body_md: '',
    status: 'published',
    version: value,
    content_hash: hash.repeat(64),
    created_at: value,
    author: 'writer',
    chunks: [{
      chunk_id: value === 1 ? 'first' : 'second',
      heading_path: ['steps'],
      line_start: 1,
      line_end: 4,
      content: `步骤 v${value}`,
      content_hash: hash.repeat(24),
      chunker_version: 'v1',
    }],
  }
}

it('keeps a citation on its fixed historical version after a newer version exists', async () => {
  route.params.version = '1'
  route.hash = ''
  vi.mocked(api.request).mockResolvedValue(version(1, 'a') as never)
  const root = node('root')
  const app = renderer.createApp(component())
  app.provide(ssrContextKey, { modules: new Set() })
  app.mount(root)
  await flush()

  expect(text(root)).toContain('Runbook v1')
  expect(vi.mocked(api.request)).toHaveBeenCalledWith(
    `/v1/kb/entries/${route.params.id}/versions/1`,
  )

  // The list endpoint may already expose v2, but the fixed-version view must
  // still request the immutable v1 route.
  route.params.version = '1'
  route.params.id = route.params.id
  await flush()
  expect(text(root)).toContain('Runbook v1')
  expect(vi.mocked(api.request)).toHaveBeenLastCalledWith(
    `/v1/kb/entries/${route.params.id}/versions/1`,
  )
  app.unmount()
})

it('scrolls to the exact chunk when only the citation hash changes', async () => {
  route.params.version = '1'
  route.hash = '#first'
  vi.mocked(api.request).mockResolvedValue(version(1, 'a') as never)
  const seen: string[] = []
  vi.stubGlobal('document', {
    getElementById: (id: string) => ({
      scrollIntoView: () => seen.push(id),
    }),
  })
  const root = node('root')
  const app = renderer.createApp(component())
  app.provide(ssrContextKey, { modules: new Set() })
  app.mount(root)
  await flush()
  expect(seen.at(-1)).toBe('first')

  route.hash = '#second'
  await flush()
  expect(seen.at(-1)).toBe('second')
  app.unmount()
  vi.unstubAllGlobals()
})

it('drops a stale version response after the reader identity changes', async () => {
  let resolveOld!: (value: unknown) => void
  const old = new Promise(resolve => { resolveOld = resolve })
  vi.mocked(api.request)
    .mockImplementationOnce(async () => await old as never)
    .mockResolvedValueOnce(version(1, 'b') as never)
  route.params.version = '1'
  route.hash = ''
  bearerToken.value = 'old-reader'
  const root = node('root')
  const app = renderer.createApp(component())
  app.provide(ssrContextKey, { modules: new Set() })
  app.mount(root)
  await flush()

  bearerToken.value = 'new-reader'
  await flush()
  resolveOld(version(1, 'a'))
  await flush()

  // The identity-change test is about discarding the old request result. The
  // current reader's replacement request may succeed independently.
  expect(text(root)).toContain('Runbook v1')
  expect(text(root)).not.toContain('内容哈希aaaa')
  expect(text(root)).toContain('内容哈希bbbb')
  app.unmount()
})
