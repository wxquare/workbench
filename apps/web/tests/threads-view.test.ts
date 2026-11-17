import * as Vue from 'vue'
import { createRenderer, nextTick, ssrContextKey } from 'vue'
import { compile } from '@vue/compiler-dom'
import { compileScript, parse } from '@vue/compiler-sfc'
import { expect, it, vi } from 'vitest'
import Threads from '../src/views/Threads.vue'
import source from '../src/views/Threads.vue?raw'
import { api } from '../src/session'

vi.mock('../src/session', async () => {
  const { ref } = await import('vue')
  return { bearerToken: ref('token'), api: { agents: vi.fn(), request: vi.fn(), run: vi.fn() } }
})

interface Node {
  type: string; text: string; children: Node[]; parent: Node | null
  props: Record<string, any>; value: string; selected: boolean
  readonly options: Node[]
  addEventListener(event: string, listener: Function): void
  getRootNode(): object
}
function node(type: string, text = ''): Node {
  return { type, text, children: [], parent: null, props: {}, value: '', selected: false,
    get options() { return this.children.filter(item => item.type === 'option') },
    addEventListener() {}, getRootNode() { return {} } }
}
const renderer = createRenderer<Node, Node>({
  createElement: type => node(type), createText: text => node('#text', text), createComment: text => node('#comment', text),
  setText: (el, text) => { el.text = text }, setElementText: (el, text) => { el.text = text; el.children = [] },
  insert: (el, parent, anchor) => { el.parent = parent; const index = anchor ? parent.children.indexOf(anchor) : -1; if (index < 0) parent.children.push(el); else parent.children.splice(index, 0, el) },
  remove: el => { if (el.parent) el.parent.children.splice(el.parent.children.indexOf(el), 1) },
  parentNode: el => el.parent, nextSibling: el => el.parent?.children[el.parent.children.indexOf(el) + 1] ?? null,
  patchProp: (el, key, _old, value) => { el.props[key] = value; if (key === 'value') el.value = value },
})
function all(root: Node): Node[] { return [root, ...root.children.flatMap(all)] }
function contents(el: Node): string { return el.text + el.children.map(contents).join('') }
function find(root: Node, type: string, text = ''): Node {
  const result = all(root).find(item => item.type === type && contents(item).includes(text))
  if (!result) throw new Error(`Missing ${type}: ${text}`)
  return result
}
async function flush() { for (let i = 0; i < 8; i++) { await Promise.resolve(); await nextTick() } }

it('clears the old agent conversation while loading another agent and submits only the current thread', async () => {
  vi.stubGlobal('Document', class {})
  vi.stubGlobal('ShadowRoot', class {})
  vi.stubGlobal('crypto', { randomUUID: () => 'conversation-key' })
  vi.mocked(api.agents).mockResolvedValue([
    { agent_id: 'dod-agent', name: 'DoD Helper' }, { agent_id: 'writing-agent', name: 'Writing Helper' },
  ])
  const thread = (agent: string) => ({ agent_id: `${agent}-agent`, thread_id: `${agent}-thread`, title: `${agent} conversation`,
    status: 'succeeded', last_run_id: `${agent}-run`, created_at: '2026-11-17T00:00:00Z', updated_at: '2026-11-17T00:00:00Z' })
  let resolveWriting!: (page: any) => void
  vi.mocked(api.request).mockImplementation(async (path, init) => {
    if (init?.method === 'POST') return { run_id: 'writing-run', thread_id: 'writing-thread' } as never
    if (path.startsWith('/v1/threads?agent_id=dod-agent')) return { items: [thread('dod')], next_cursor: null } as never
    if (path.startsWith('/v1/threads?agent_id=writing-agent'))
      return await new Promise(resolve => { resolveWriting = resolve }) as never
    if (path.includes('/messages?')) return { items: [], next_cursor: null } as never
    throw new Error(`Unexpected path: ${path}`)
  })
  vi.mocked(api.run).mockResolvedValue({ run_id: 'writing-run', agent_id: 'writing-agent', thread_id: 'writing-thread',
    status: 'succeeded', phase: null, outcome: { summary: 'Edited' }, waiting: null, created_at: '2026-11-17T00:00:00Z' })
  const descriptor = parse(source).descriptor
  const bindings = compileScript(descriptor, { id: 'threads-test' }).bindings
  const code = compile(descriptor.template!.content, { mode: 'function', prefixIdentifiers: true, bindingMetadata: bindings }).code
  const root = node('root'), app = renderer.createApp({ ...Threads, render: new Function('Vue', code)(Vue) })
  app.component('RouterLink', { render() { return Vue.h('a', this.$slots.default?.()) } })
  app.provide(ssrContextKey, { modules: new Set() })
  try {
    app.mount(root); await flush()
    const select = find(root, 'select')
    select.props['onUpdate:modelValue']('dod-agent'); await flush()
    find(root, 'button', 'dod conversation').props.onClick(); await flush()
    select.props['onUpdate:modelValue']('writing-agent'); await flush()
    expect(contents(root)).not.toContain('dod conversation')
    expect(contents(root)).not.toContain('会话详情')
    resolveWriting({ items: [thread('writing')], next_cursor: null }); await flush()
    find(root, 'button', 'writing conversation').props.onClick(); await flush()
    find(root, 'textarea').props['onUpdate:modelValue']('Edit this paragraph.'); await flush()
    find(root, 'form').props.onSubmit({ preventDefault() {} }); await flush()
    const posts = vi.mocked(api.request).mock.calls.filter(([, init]) => init?.method === 'POST')
    expect(posts).toHaveLength(1)
    expect(posts[0][0]).toBe('/v1/runs')
    expect(JSON.parse(posts[0][1]!.body as string)).toEqual({ agent_id: 'writing-agent', thread_id: 'writing-thread',
      mode: 'qa', input: { text: 'Edit this paragraph.' }, business_context: { conversation: true } })
  } finally {
    app.unmount()
    vi.unstubAllGlobals()
  }
})
