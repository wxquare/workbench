import * as Vue from 'vue'
import { createRenderer, nextTick, ssrContextKey } from 'vue'
import { compile } from '@vue/compiler-dom'
import { compileScript, parse } from '@vue/compiler-sfc'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Models from '../src/views/Models.vue'
import modelsSource from '../src/views/Models.vue?raw'
import Workbench from '../src/views/Workbench.vue'
import workbenchSource from '../src/views/Workbench.vue?raw'
import Threads from '../src/views/Threads.vue'
import threadsSource from '../src/views/Threads.vue?raw'
import { api } from '../src/session'
import type { ModelProfile } from '../src/models-api'

vi.mock('../src/session', async () => {
  const { ref } = await import('vue')
  return { bearerToken: ref('token'), api: { agents: vi.fn(), request: vi.fn(), run: vi.fn(), createQa: vi.fn() } }
})

interface Node {
  type: string; text: string; children: Node[]; parent: Node | null
  props: Record<string, any>; value: string; selected: boolean
  listeners: Record<string, Function>; readonly options: Node[]
  addEventListener(event: string, listener: Function): void
  getRootNode(): object
}
function node(type: string, text = ''): Node {
  return { type, text, children: [], parent: null, props: {}, value: '', selected: false, listeners: {},
    get options() { return this.children.filter(item => item.type === 'option') },
    addEventListener(event, listener) { this.listeners[event] = listener }, getRootNode() { return {} } }
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
async function switchAgent(select: Node, value: string) {
  if (select.props.disabled) throw new Error('Agent selector is disabled')
  select.value = value
  for (const option of select.options) option.selected = option.value === value
  select.listeners.change?.({ target: select })
  select.props.onChange?.({ target: select })
  await flush()
}
function deferred<T>() {
  let resolve!: (value: T) => void, reject!: (error: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const apps: Vue.App[] = []
function mount(component: Vue.Component, source: string, router?: ReturnType<typeof createRouter>) {
  const descriptor = parse(source).descriptor
  const bindings = compileScript(descriptor, { id: 'agent-switch-test' }).bindings
  const code = compile(descriptor.template!.content, { mode: 'function', prefixIdentifiers: true, bindingMetadata: bindings }).code
  const root = node('root'), app = renderer.createApp({ ...component, render: new Function('Vue', code)(Vue) })
  if (router) app.use(router)
  else app.component('RouterLink', { render() { return Vue.h('a', this.$slots.default?.()) } })
  app.provide(ssrContextKey, { modules: new Set() })
  apps.push(app); app.mount(root)
  return root
}
beforeEach(() => {
  vi.resetAllMocks()
  vi.stubGlobal('Document', class {})
  vi.stubGlobal('ShadowRoot', class {})
  vi.stubGlobal('crypto', { randomUUID: () => 'switch-test-key' })
  vi.mocked(api.agents).mockResolvedValue([
    { agent_id: 'dod-agent', name: 'DoD Helper' }, { agent_id: 'writing-agent', name: 'Writing Helper' },
  ])
})
afterEach(() => {
  for (const app of apps.splice(0)) app.unmount()
  vi.unstubAllGlobals()
})

const profile = (name: string, model: string): ModelProfile => ({
  profile_id: `${name}-profile`, name, revision: 1, active_version: null,
  draft: { name, provider: 'deepseek', adapter: 'deepseek', protocol: 'openai_chat_completions',
    base_url: 'https://api.deepseek.com', model_id: model, secret_ref: 'env://DEEPSEEK_API_KEY',
    temperature: 0, request_timeout_seconds: 60, max_output_tokens: 4096, max_context_tokens: 64000,
    streaming: true, sdk_max_retries: 0, invocation_max_attempts: 3,
    structured_output_strategy: 'json_mode', provider_options: { thinking: { type: 'disabled' } } },
})
function mockProfiles() {
  vi.mocked(api.request).mockImplementation(async path => {
    if (path.startsWith('/v1/agents/dod-agent/model-profiles?'))
      return { items: [profile('dod-model', 'dod-base')], next_cursor: null } as never
    if (path.startsWith('/v1/agents/writing-agent/model-profiles?'))
      return { items: [profile('writing-model', 'writing-base')], next_cursor: null } as never
    throw new Error(`Unexpected path: ${path}`)
  })
}

it.each(['existing', 'new'])('preserves an unsaved %s model when an Agent switch is canceled', async kind => {
  mockProfiles()
  const confirm = vi.fn().mockReturnValue(false)
  vi.stubGlobal('window', { confirm })
  const root = mount(Models, modelsSource)
  await flush()
  const select = find(root, 'select')
  await switchAgent(select, 'dod-agent')
  find(root, 'button', kind === 'existing' ? 'dod-model' : '新建模型').props.onClick()
  await flush()
  find(find(root, 'label', '模型 ID'), 'input').props['onUpdate:modelValue']('unsaved-model')
  await flush()
  await switchAgent(select, 'writing-agent')
  expect(confirm).toHaveBeenCalledOnce()
  expect(select.value).toBe('dod-agent')
  expect(find(find(root, 'label', '模型 ID'), 'input').value).toBe('unsaved-model')
  expect(contents(root)).not.toContain('writing-model')
  expect(api.request).toHaveBeenCalledTimes(1)

  confirm.mockReturnValue(true)
  await switchAgent(select, 'writing-agent')
  expect(select.value).toBe('writing-agent')
  expect(contents(root)).not.toContain('dod-model')
  find(root, 'button', 'writing-model').props.onClick(); await flush()
  expect(find(find(root, 'label', '模型 ID'), 'input').value).toBe('writing-base')
})

it('switches clean model configurations without a discard prompt', async () => {
  mockProfiles()
  const confirm = vi.fn()
  vi.stubGlobal('window', { confirm })
  const root = mount(Models, modelsSource)
  await flush()
  const select = find(root, 'select')
  await switchAgent(select, 'dod-agent')
  find(root, 'button', 'dod-model').props.onClick(); await flush()
  await switchAgent(select, 'writing-agent')
  expect(confirm).not.toHaveBeenCalled()
  expect(contents(root)).toContain('writing-model')
  expect(contents(root)).not.toContain('dod-model')
})

it('saves a masked model key without displaying it', async () => {
  const saved = profile('dod-model', 'dod-base')
  saved.revision = 2
  saved.draft.secret_ref = 'secret://managed-model/0123456789abcdef0123456789abcdef'
  vi.mocked(api.request).mockImplementation(async (path, init) => {
    if (path.startsWith('/v1/agents/dod-agent/model-profiles?'))
      return { items: [profile('dod-model', 'dod-base')], next_cursor: null } as never
    if (path.endsWith('/credential')) {
      expect(init?.method).toBe('PUT')
      expect(JSON.parse(init?.body as string)).toEqual({ api_key: 'fictional-key' })
      return saved as never
    }
    throw new Error(`Unexpected path: ${path}`)
  })
  const root = mount(Models, modelsSource)
  await flush()
  await switchAgent(find(root, 'select'), 'dod-agent')
  find(root, 'button', 'dod-model').props.onClick(); await flush()
  const input = find(find(root, 'label', 'API Key'), 'input')
  expect(input.props.type).toBe('password')
  input.props['onUpdate:modelValue']('fictional-key'); await flush()
  await find(root, 'button', '保存密钥').props.onClick(); await flush()
  expect(input.value).toBe('')
  expect(contents(root)).not.toContain('fictional-key')
  expect(contents(root)).toContain('密钥已保存至草稿')
})

it.each(['accepted', 'failed'])('locks Agent selection and refresh until QA submission is %s', async result => {
  const pending = deferred<{ run_id: string }>()
  vi.mocked(api.createQa).mockReturnValue(pending.promise)
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/', component: Workbench }, { path: '/runs/:id', name: 'run-detail', component: { render: () => null } },
  ] })
  await router.push('/')
  const root = mount(Workbench, workbenchSource, router)
  await flush()
  const select = find(root, 'select')
  await switchAgent(select, 'dod-agent')
  find(root, 'textarea').props['onUpdate:modelValue']('Check the incident.'); await flush()
  const submission = find(root, 'form').props.onSubmit({ preventDefault() {} })
  await flush()
  expect(select.props.disabled).toBe(true)
  expect(find(root, 'button', '刷新 Agent').props.disabled).toBe(true)
  expect(api.createQa).toHaveBeenCalledExactlyOnceWith({ agent_id: 'dod-agent', text: 'Check the incident.' }, 'switch-test-key')
  if (result === 'accepted') pending.resolve({ run_id: 'dod-run' })
  else pending.reject(new Error('Submission unavailable'))
  await submission; await flush()
  expect(select.props.disabled).toBe(false)
  expect(find(root, 'button', '刷新 Agent').props.disabled).toBe(false)
  if (result === 'accepted') expect(router.currentRoute.value.fullPath).toBe('/runs/dod-run')
  else {
    expect(contents(root)).toContain('Submission unavailable')
    expect(router.currentRoute.value.fullPath).toBe('/')
    await switchAgent(select, 'writing-agent')
    expect(select.value).toBe('writing-agent')
  }
})

it.each([true, false])('scopes terminal conversation completion to its Agent (switch: %s)', async switchAway => {
  const finalMessages = deferred<{ items: []; next_cursor: null }>()
  let messageLoads = 0
  const thread = (agent: string) => ({ agent_id: `${agent}-agent`, thread_id: `${agent}-thread`, title: `${agent} conversation`,
    status: 'running', last_run_id: `${agent}-run`, created_at: '2026-11-17T00:00:00Z', updated_at: '2026-11-17T00:00:00Z' })
  vi.mocked(api.request).mockImplementation(async path => {
    if (path.startsWith('/v1/threads?agent_id=dod-agent')) return { items: [thread('dod')], next_cursor: null } as never
    if (path.startsWith('/v1/threads?agent_id=writing-agent')) return { items: [thread('writing')], next_cursor: null } as never
    if (path.startsWith('/v1/threads/dod-thread/messages?')) {
      if (++messageLoads === 2) return await finalMessages.promise as never
      return { items: [], next_cursor: null } as never
    }
    throw new Error(`Unexpected path: ${path}`)
  })
  vi.mocked(api.run).mockResolvedValue({ run_id: 'dod-run', agent_id: 'dod-agent', thread_id: 'dod-thread',
    status: 'failed', phase: null, outcome: null, waiting: null, created_at: '2026-11-17T00:00:00Z' })
  const root = mount(Threads, threadsSource)
  await flush()
  const select = find(root, 'select')
  await switchAgent(select, 'dod-agent')
  find(root, 'button', 'dod conversation').props.onClick(); await flush()
  expect(messageLoads).toBe(2)
  if (switchAway) await switchAgent(select, 'writing-agent')
  finalMessages.resolve({ items: [], next_cursor: null }); await flush()
  if (switchAway) {
    expect(contents(root)).not.toContain('任务状态：failed')
    expect(contents(root)).toContain('writing conversation')
    expect(contents(root)).not.toContain('dod conversation')
    expect(vi.mocked(api.request).mock.calls.filter(([path]) => path.startsWith('/v1/threads?agent_id=writing-agent'))).toHaveLength(1)
  } else {
    expect(contents(root)).toContain('任务状态：failed')
    expect(vi.mocked(api.request).mock.calls.filter(([path]) => path.startsWith('/v1/threads?agent_id=dod-agent'))).toHaveLength(2)
  }
})
