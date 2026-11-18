import * as Vue from 'vue'
import { createRenderer, nextTick, ssrContextKey } from 'vue'
import { compile } from '@vue/compiler-dom'
import { compileScript, parse } from '@vue/compiler-sfc'
import { expect, it, vi } from 'vitest'
import RunDetail from '../src/views/RunDetail.vue'
import source from '../src/views/RunDetail.vue?raw'
vi.mock('vue-router', () => ({ useRoute: () => ({ params: { id: 'replay' } }) }))
vi.mock('../src/session', async () => { const { ref } = await import('vue'); return { bearerToken: ref('token'), api: { agents: vi.fn().mockResolvedValue([]), request: vi.fn().mockResolvedValue({ items: [], next_cursor: null }) } } })
vi.mock('../src/components/RunActions.vue', () => ({ default: { render: () => null } }))
vi.mock('../src/composables/useRunDetail', async () => {
  const { ref } = await import('vue')
  return { useRunDetail: () => ({ run: ref({ run_id: 'replay', status: 'succeeded', created_at: 0, links: { knowledge_gap: '/v1/kb/gaps/gap-123' } }), error: ref(''), loading: ref(false), connection: ref('closed'), draft: ref(''), waiting: ref(null), refresh: vi.fn(), applyRunView: vi.fn() }) }
})
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
  const bindings = compileScript(descriptor, { id: 'run-gap-test' }).bindings
  const code = compile(descriptor.template!.content, { mode: 'function', prefixIdentifiers: true, bindingMetadata: bindings }).code
  return { ...RunDetail, render: new Function('Vue', code)(Vue) }
}

it('links a replay to its particular knowledge gap', async () => {
  const links: unknown[] = []
  const root = node('root'), app = renderer.createApp(component())
  app.component('router-link', { props: ['to'], setup(props, { slots }) { links.push(props.to); return () => Vue.h('a', slots.default?.()) } })
  app.provide(ssrContextKey, { modules: new Set() }); app.mount(root); await flush()
  expect(links).toContainEqual({ name: 'knowledge', query: { gap: 'gap-123' } })
  app.unmount()
})
