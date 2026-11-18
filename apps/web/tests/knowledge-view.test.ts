import * as Vue from 'vue'
import { createRenderer, nextTick, ssrContextKey } from 'vue'
import { compile } from '@vue/compiler-dom'
import { compileScript, parse } from '@vue/compiler-sfc'
import { expect, it, vi } from 'vitest'
import Knowledge from '../src/views/Knowledge.vue'
import KnowledgeEntry from '../src/views/KnowledgeEntry.vue'
import entrySource from '../src/views/KnowledgeEntry.vue?raw'
import source from '../src/views/Knowledge.vue?raw'
import { api, bearerToken } from '../src/session'

vi.mock('../src/session', async () => {
  const { ref } = await import('vue')
  return { bearerToken: ref(''), api: { request: vi.fn() } }
})
const route = Vue.reactive({ query: {} as Record<string, string>, params: { id: 'entry', version: '1' }, hash: '' })
vi.mock('vue-router', () => ({ onBeforeRouteLeave: vi.fn(), useRouter: () => ({ push: vi.fn() }), useRoute: () => route }))

interface Node { type: string; text: string; children: Node[]; parent: Node | null; props: Record<string, any>; listeners: Record<string, Function>; value: string; addEventListener(event: string, listener: Function): void; getRootNode(): object }
function node(type: string, text = ''): Node {
  const value: Node = { type, text, children: [], parent: null, props: {}, listeners: {}, value: '', getRootNode() { return {} }, addEventListener(event, listener) { value.listeners[event] = listener } }
  Object.defineProperty(value, 'options', { get: () => value.children.filter(child => child.type === 'option') })
  return value
}
const renderer = createRenderer<Node, Node>({
  createElement: type => node(type), createText: text => node('#text', text), createComment: text => node('#comment', text),
  setText: (el, text) => { el.text = text }, setElementText: (el, text) => { el.text = text; el.children = [] },
  insert: (el, parent, anchor) => { el.parent = parent; const i = anchor ? parent.children.indexOf(anchor) : -1; if (i < 0) parent.children.push(el); else parent.children.splice(i, 0, el) },
  remove: el => { if (el.parent) el.parent.children.splice(el.parent.children.indexOf(el), 1) },
  parentNode: el => el.parent, nextSibling: el => el.parent?.children[el.parent.children.indexOf(el) + 1] ?? null,
  patchProp: (el, key, _old, value) => { el.props[key] = value },
})
function all(root: Node): Node[] { return [root, ...root.children.flatMap(all)] }
function text(el: Node): string { return el.text + el.children.map(text).join('') }
function find(root: Node, type: string, label: string): Node {
  const found = all(root).find(el => el.type === type && text(el).includes(label))
  if (!found) throw new Error(`missing ${type}: ${label}`)
  return found
}
async function flush() { for (let i = 0; i < 8; i++) { await Promise.resolve(); await nextTick() } }
function component() {
  const descriptor = parse(source).descriptor
  const bindings = compileScript(descriptor, { id: 'knowledge-test' }).bindings
  const code = compile(descriptor.template!.content, { mode: 'function', prefixIdentifiers: true, bindingMetadata: bindings }).code
  return { ...Knowledge, render: new Function('Vue', code)(Vue) }
}

it('creates a draft then publishes the saved revision', async () => {
  vi.stubGlobal('Document', class {}); vi.stubGlobal('ShadowRoot', class {})
  vi.stubGlobal('crypto', { randomUUID: () => 'test-key' }); vi.stubGlobal('window', { confirm: () => true })
  bearerToken.value = 'token'
  const draft = { title: '库存排查', kind: 'faq', body_md: '检查库存。', service: null, environment: null, region: null, tags: [], aliases: [], review_by: null }
  const item = { entry_id: 'entry', ...draft, draft, status: 'draft', revision: 0, published_version: null, has_unpublished_changes: false, owner: 'admin', created_at: 0, updated_at: 0 }
  vi.mocked(api.request).mockImplementation(async (path, init) => {
    if (path.startsWith('/v1/kb/entries?')) return { items: [], next_cursor: null } as never
    if (path === '/v1/kb/entries' && init?.method === 'POST') return item as never
    if (path.endsWith('/publish')) return { entry_id: 'entry', version: 1, content_hash: 'hash', created_at: 0 } as never
    if (path.endsWith('/versions')) return { items: [], next_cursor: null } as never
    return { ...item, status: 'published', revision: 1, published_version: 1 } as never
  })
  const root = node('root'), app = renderer.createApp(component())
  app.provide(ssrContextKey, { modules: new Set() }); app.mount(root); await flush()
  find(root, 'button', '新建条目').props.onClick(); await flush()
  for (const [label, value] of [['条目标题', '库存排查'], ['Markdown 正文', '检查库存。']]) {
    const field = find(root, 'label', label)
    const input = all(field).find(el => el.type === 'input' || el.type === 'textarea')!
    input.value = value; input.listeners.input({ target: input })
  }
  await flush(); find(root, 'button', '保存草稿').props.onClick(); await flush()
  expect(vi.mocked(api.request).mock.calls.some(([path, init]) => path === '/v1/kb/entries' && JSON.parse(init!.body as string).body_md === '检查库存。')).toBe(true)
  find(root, 'button', '发布版本').props.onClick(); await flush()
  expect(vi.mocked(api.request).mock.calls.some(([path, init]) => path === '/v1/kb/entries/entry/publish' && (init!.headers as Record<string, string>)['If-Match'] === '0')).toBe(true)
  app.unmount()
})

function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(r => { resolve = r }); return { promise, resolve } }
function setupGlobals() {
  vi.stubGlobal('Document', class {}); vi.stubGlobal('ShadowRoot', class {})
  vi.stubGlobal('crypto', { randomUUID: () => 'test-key' }); vi.stubGlobal('window', { confirm: () => true })
}
function fixture(title = '旧团队草稿') {
  const draft = { title, kind: 'faq', body_md: '正文', service: null, environment: null, region: null, tags: [], aliases: [], review_by: null }
  return { entry_id: 'entry', ...draft, draft, status: 'published', revision: 1, published_version: 2, has_unpublished_changes: false, owner: 'admin', created_at: 0, updated_at: 0 }
}
function mountKnowledge() { const root = node('root'), app = renderer.createApp(component()); app.provide(ssrContextKey, { modules: new Set() }); app.mount(root); return { root, app } }

it('discards a rollback refresh that completes after identity changes', async () => {
  setupGlobals(); bearerToken.value = 'old'
  const old = fixture(), refresh = deferred<unknown>()
  let gets = 0
  vi.mocked(api.request).mockImplementation(async path => {
    if (path.startsWith('/v1/kb/entries?')) return { items: bearerToken.value === 'old' ? [old] : [], next_cursor: null } as never
    if (path.endsWith('/versions')) return { items: [{ version: 1 }], next_cursor: null } as never
    if (path.endsWith('/rollback')) return { version: 3 } as never
    if (path === '/v1/kb/entries/entry') return (++gets === 1 ? old : await refresh.promise) as never
    throw new Error(path)
  })
  const { root, app } = mountKnowledge(); await flush()
  find(root, 'button', old.title).props.onClick(); await flush()
  find(root, 'button', '版本历史').props.onClick(); await flush()
  find(root, 'button', '重新发布').props.onClick(); await flush()
  bearerToken.value = 'new'; await flush(); refresh.resolve(old); await flush()
  expect(text(root)).not.toContain(old.title)
  app.unmount()
})

it('discards a file read after changing identity', async () => {
  setupGlobals(); bearerToken.value = 'old'
  vi.mocked(api.request).mockResolvedValue({ items: [], next_cursor: null } as never)
  const { root, app } = mountKnowledge(); await flush()
  find(root, 'button', '新建条目').props.onClick(); await flush()
  const file = deferred<ArrayBuffer>(), input = all(root).find(el => el.type === 'input' && el.props.type === 'file')!
  input.props.onChange({ target: { files: [{ name: 'private.txt', size: 10, arrayBuffer: () => file.promise }], value: '' } })
  bearerToken.value = 'new'; await flush(); find(root, 'button', '新建条目').props.onClick(); await flush()
  file.resolve(new TextEncoder().encode('旧团队内容').buffer as ArrayBuffer); await flush()
  expect(text(root)).not.toContain('private')
  const textarea = all(root).find(el => el.type === 'textarea')!
  expect(textarea.value).not.toContain('旧团队内容')
  app.unmount()
})

it('finishes the entry list load when search starts while it is pending', async () => {
  setupGlobals(); bearerToken.value = 'token'
  const list = deferred<unknown>(), old = fixture()
  vi.mocked(api.request).mockImplementation(async path => path.startsWith('/v1/kb/entries?') ? await list.promise as never : { matches: [] } as never)
  const { root, app } = mountKnowledge(); await flush()
  find(root, 'button', '检索调试').props.onClick(); await flush()
  for (const label of ['模拟 Agent 的问题', '服务', '环境', '区域']) {
    const input = all(find(root, 'label', label)).find(el => el.type === 'input')!
    input.value = 'x'; input.listeners.input({ target: input })
  }
  await flush(); find(root, 'button', '测试检索').props.onClick(); await flush()
  list.resolve({ items: [old], next_cursor: null }); await flush()
  find(root, 'button', '知识条目').props.onClick(); await flush()
  expect(text(root)).toContain(old.title)
  expect(find(root, 'button', '新建条目').props.disabled).toBeFalsy()
  app.unmount()
})


it('scrolls to a different chunk when only the citation hash changes', async () => {
  setupGlobals(); bearerToken.value = 'token'; route.hash = '#first'
  const scroll = vi.fn()
  vi.stubGlobal('document', { getElementById: (id: string) => ({ scrollIntoView: () => scroll(id) }) })
  vi.mocked(api.request).mockResolvedValue({ title: '版本', version: 1, chunks: [], status: 'published' } as never)
  const descriptor = parse(entrySource).descriptor
  const bindings = compileScript(descriptor, { id: 'entry-test' }).bindings
  const code = compile(descriptor.template!.content, { mode: 'function', prefixIdentifiers: true, bindingMetadata: bindings }).code
  const root = node('root'), app = renderer.createApp({ ...KnowledgeEntry, render: new Function('Vue', code)(Vue) })
  app.provide(ssrContextKey, { modules: new Set() }); app.mount(root); await flush()
  route.hash = '#second'; await flush()
  expect(scroll).toHaveBeenCalledWith('second')
  app.unmount(); route.hash = ''; vi.unstubAllGlobals()
})
