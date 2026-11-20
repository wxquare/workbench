import * as Vue from 'vue'
import { createRenderer, nextTick, ssrContextKey } from 'vue'
import { compile } from '@vue/compiler-dom'
import { compileScript, parse } from '@vue/compiler-sfc'
import { beforeEach, expect, it, vi } from 'vitest'
import RunDetail from '../src/views/RunDetail.vue'
import source from '../src/views/RunDetail.vue?raw'
import { api, bearerToken } from '../src/session'

vi.mock('../src/components/RunActions.vue', () => ({ default: { render: () => null } }))
vi.mock('../src/components/RunFeedback.vue', () => ({ default: { render: () => null } }))
vi.mock('../src/components/RunUsage.vue', () => ({ default: { render: () => null } }))

vi.mock('vue-router', () => ({ useRoute: () => ({ params: { id: 'run-1' } }) }))
vi.mock('../src/session', async () => {
  const { ref } = await import('vue')
  return {
    bearerToken: ref('token'),
    api: { run: vi.fn(), agents: vi.fn(), usage: vi.fn() },
  }
})

interface Node { type: string; text: string; children: Node[]; parent: Node | null; props: Record<string, unknown> }
function node(type: string, text = ''): Node { return { type, text, children: [], parent: null, props: {} } }
const renderer = createRenderer<Node, Node>({
  createElement: type => node(type), createText: text => node('#text', text), createComment: text => node('#comment', text),
  setText: (el, text) => { el.text = text }, setElementText: (el, text) => { el.text = text; el.children = [] },
  insert: (el, parent, anchor) => { el.parent = parent; const index = anchor ? parent.children.indexOf(anchor) : -1; if (index < 0) parent.children.push(el); else parent.children.splice(index, 0, el) },
  remove: el => { if (el.parent) el.parent.children.splice(el.parent.children.indexOf(el), 1) },
  parentNode: el => el.parent, nextSibling: el => el.parent?.children[el.parent.children.indexOf(el) + 1] ?? null,
  patchProp: (el, key, _old, value) => { el.props[key] = value },
})
function contents(el: Node): string { return el.text + el.children.map(contents).join('') }
async function flush() { for (let i = 0; i < 6; i++) { await Promise.resolve(); await nextTick() } }
function mount() {
  const descriptor = parse(source).descriptor
  const bindings = compileScript(descriptor, { id: 'run-agent-test' }).bindings
  const code = compile(descriptor.template!.content, { mode: 'function', prefixIdentifiers: true, bindingMetadata: bindings }).code
  const root = node('root')
  const app = renderer.createApp({ ...RunDetail, render: new Function('Vue', code)(Vue) })
  app.component('RouterLink', { template: '<a><slot /></a>' })
  app.provide(ssrContextKey, { modules: new Set() })
  app.mount(root)
  return { app, root }
}

beforeEach(() => {
  bearerToken.value = 'token'
  vi.mocked(api.run).mockReset().mockResolvedValue({ run_id: 'run-1', agent_id: 'ws-2', thread_id: 'thread', status: 'succeeded', phase: null, created_at: '2026-11-16T00:00:00Z', outcome: null, waiting: null })
  vi.mocked(api.agents).mockReset()
  vi.mocked(api.usage).mockReset().mockResolvedValue({})
})

it('shows the agent name and persisted id associated with a run', async () => {
  vi.mocked(api.agents).mockResolvedValue([{ agent_id: 'ws-1', name: '其他助手' }, { agent_id: 'ws-2', name: '对话助手' }])
  const { app, root } = mount()
  await flush()
  expect(contents(root)).toContain('对话助手')
  expect(contents(root)).toContain('ws-2')
  expect(contents(root)).not.toContain('其他助手')
  app.unmount()
})

it('falls back to the persisted id if the agent list cannot be loaded', async () => {
  vi.mocked(api.agents).mockRejectedValue(new Error('unavailable'))
  const { app, root } = mount()
  await flush()
  expect(contents(root)).toContain('ws-2')
  expect(contents(root)).toContain('已完成')
  app.unmount()
})

it('ignores an old agent list after the bearer identity changes', async () => {
  let resolveOld!: (items: { agent_id: string; name: string }[]) => void
  vi.mocked(api.agents).mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve }))
    .mockResolvedValueOnce([{ agent_id: 'ws-2', name: '当前助手' }])
  const { app, root } = mount()
  await flush()
  bearerToken.value = 'other-token'
  await flush()
  resolveOld([{ agent_id: 'ws-2', name: '旧身份的助手' }])
  await flush()
  expect(contents(root)).toContain('当前助手')
  expect(contents(root)).not.toContain('旧身份的助手')
  app.unmount()
})
