import * as Vue from 'vue'
import { createRenderer, nextTick, ssrContextKey } from 'vue'
import { compile } from '@vue/compiler-dom'
import { compileScript, parse } from '@vue/compiler-sfc'
import { expect, it, vi } from 'vitest'
import KnowledgeGaps from '../src/components/KnowledgeGaps.vue'
import source from '../src/components/KnowledgeGaps.vue?raw'
import { api, bearerToken } from '../src/session'

vi.mock('../src/session', async () => {
  const { ref } = await import('vue')
  return { bearerToken: ref(''), api: { request: vi.fn(), run: vi.fn() } }
})
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))

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
async function flush() { for (let i = 0; i < 8; i++) { await Promise.resolve(); await nextTick() } }
function component() {
  const descriptor = parse(source).descriptor
  const bindings = compileScript(descriptor, { id: 'gaps-test' }).bindings
  const code = compile(descriptor.template!.content, { mode: 'function', prefixIdentifiers: true, bindingMetadata: bindings }).code
  return { ...KnowledgeGaps, render: new Function('Vue', code)(Vue) }
}

it('turns the selected authorized gap into an editable empty draft', async () => {
  vi.stubGlobal('Document', class {}); vi.stubGlobal('ShadowRoot', class {})
  vi.stubGlobal('crypto', { randomUUID: () => 'test-key' }); vi.stubGlobal('window', { confirm: () => true })
  bearerToken.value = 'token'
  const gap = { gap_id: 'gap', run_id: 'run', gap_revision: 2, problem_type: 'kb.empty', review_status: 'open',
    summary: '如何处理库存？', query_scope: { service: 'stock', environment: 'test', region: 'sg' }, created_at: 0,
    resolved_entry_id: null, replay_run_id: null }
  const draft = { entry_id: 'entry', revision: 0, draft: { title: gap.summary, kind: 'faq', body_md: '' } }
  vi.mocked(api.request).mockImplementation(async (path) => path.startsWith('/v1/kb/gaps?')
    ? { items: [gap], next_cursor: null } as never : draft as never)
  const emitted = vi.fn(), root = node('root'), app = renderer.createApp(component(), { onEdit: emitted })
  app.component('router-link', { render() { return Vue.h('a', this.$slots.default?.()) } })
  app.provide(ssrContextKey, { modules: new Set() }); app.mount(root); await flush()
  const button = all(root).find(el => el.type === 'button' && text(el).includes('转为条目草稿'))!
  button.props.onClick(); await flush()
  expect(emitted).toHaveBeenCalledWith(draft)
  expect(vi.mocked(api.request).mock.calls.some(([path, init]) => path === '/v1/kb/gaps/gap/draft' && (init?.headers as Record<string, string>)['If-Match'] === '2')).toBe(true)
  bearerToken.value = ''; await flush()
  expect(text(root)).not.toContain('如何处理库存？')
  app.unmount()
})

it('opens a directly linked gap even when it is outside the first page', async () => {
  vi.stubGlobal('Document', class {}); vi.stubGlobal('ShadowRoot', class {})
  bearerToken.value = 'token'
  const target = { gap_id: 'target', run_id: 'run', gap_revision: 1, problem_type: 'kb.empty', review_status: 'resolved',
    summary: '指定的历史缺口', query_scope: null, created_at: 0, resolved_entry_id: null, replay_run_id: null }
  vi.mocked(api.request).mockImplementation(async path => path === '/v1/kb/gaps/target' ? target as never : { items: [], next_cursor: null } as never)
  const root = node('root'), app = renderer.createApp(component(), { gapId: 'target' })
  app.component('router-link', { render() { return Vue.h('a', this.$slots.default?.()) } })
  app.provide(ssrContextKey, { modules: new Set() }); app.mount(root); await flush()
  expect(text(root)).toContain('指定的历史缺口')
  expect(vi.mocked(api.request).mock.calls.some(([path]) => path.includes('status=resolved'))).toBe(true)
  app.unmount()
})
