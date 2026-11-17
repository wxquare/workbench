import * as Vue from 'vue'
import { createRenderer, nextTick, ssrContextKey } from 'vue'
import { compile } from '@vue/compiler-dom'
import { compileScript, parse } from '@vue/compiler-sfc'
import { expect, it, vi } from 'vitest'
import Skills from '../src/views/Skills.vue'
import source from '../src/views/Skills.vue?raw'
import { api, bearerToken } from '../src/session'

vi.mock('../src/session', async () => {
  const { ref } = await import('vue')
  return { bearerToken: ref(''), api: { request: vi.fn() } }
})
vi.mock('vue-router', () => ({ onBeforeRouteLeave: vi.fn() }))

interface Node { type: string; text: string; children: Node[]; parent: Node | null; props: Record<string, any>; listeners: Record<string, Function>; value: string; addEventListener(event: string, listener: Function): void; getRootNode(): object }
function node(type: string, text = ''): Node {
  const value: Node = { type, text, children: [], parent: null, props: {}, listeners: {}, value: '', getRootNode() { return {} }, addEventListener(event, listener) { value.listeners[event] = listener } }
  return value
}
const renderer = createRenderer<Node, Node>({
  createElement: type => node(type), createText: text => node('#text', text), createComment: text => node('#comment', text),
  setText: (el, text) => { el.text = text }, setElementText: (el, text) => { el.text = text; el.children = [] },
  insert: (el, parent, anchor) => { el.parent = parent; const index = anchor ? parent.children.indexOf(anchor) : -1; if (index < 0) parent.children.push(el); else parent.children.splice(index, 0, el) },
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
async function flush() { for (let i = 0; i < 6; i++) { await Promise.resolve(); await nextTick() } }
function component() {
  const descriptor = parse(source).descriptor
  const bindings = compileScript(descriptor, { id: 'skills-test' }).bindings
  const code = compile(descriptor.template!.content, { mode: 'function', prefixIdentifiers: true, bindingMetadata: bindings }).code
  return { ...Skills, render: new Function('Vue', code)(Vue) }
}

it('creates and publishes a shared skill through the editor', async () => {
  vi.stubGlobal('Document', class {})
  vi.stubGlobal('ShadowRoot', class {})
  vi.stubGlobal('crypto', { randomUUID: () => 'key' })
  vi.stubGlobal('window', { confirm: () => true })
  bearerToken.value = 'token'
  const skill = { skill_id: 'id', name: 'analysis', description: 'Analyse', revision: 0, versions: [], latest_version: null, draft: { description: 'Analyse', content: 'Use evidence.' } }
  vi.mocked(api.request).mockImplementation(async (path, init) => {
    if (path.startsWith('/v1/skills?')) return { items: [], next_cursor: null } as never
    if (init?.method === 'POST' && path === '/v1/skills') return skill as never
    if (init?.method === 'POST' && path.endsWith('/publish')) return { version: '1' } as never
    return { ...skill, revision: 1 } as never
  })
  const root = node('root'), app = renderer.createApp(component())
  app.component('el-icon', { render() { return Vue.h('span', this.$slots.default?.()) } })
  app.provide(ssrContextKey, { modules: new Set() }); app.mount(root)
  await flush(); find(root, 'button', '新建 Skill').props.onClick(); await flush()
  for (const [label, value] of [['技能标识', 'analysis'], ['描述', 'Analyse'], ['技能指令', 'Use evidence.']]) {
    const container = find(root, 'label', label)
    const input = all(container).find(el => el.type === 'input' || el.type === 'textarea')!
    input.value = value; input.listeners.input({ target: input })
  }
  await flush(); find(root, 'button', '保存草稿').props.onClick(); await flush()
  expect(vi.mocked(api.request).mock.calls.some(([path, init]) => path === '/v1/skills' && JSON.parse(init!.body as string).name === 'analysis')).toBe(true)
  find(root, 'button', '发布').props.onClick(); await flush()
  expect(vi.mocked(api.request).mock.calls.some(([path, init]) => path === '/v1/skills/id/publish' && init?.headers && (init.headers as Record<string,string>)['If-Match'] === '0')).toBe(true)
  app.unmount()
})

it('disables the editor while switching skills', async () => {
  vi.stubGlobal('Document', class {})
  vi.stubGlobal('ShadowRoot', class {})
  vi.stubGlobal('window', { confirm: () => true })
  bearerToken.value = 'token'
  const skill = { skill_id: 'id', name: 'analysis', description: 'Analyse', revision: 0, versions: [], latest_version: null, draft: { description: 'Analyse', content: 'Use evidence.' } }
  let finish!: (value: typeof skill) => void
  vi.mocked(api.request).mockImplementation((path) => {
    if (path.startsWith('/v1/skills?')) return Promise.resolve({ items: [skill], next_cursor: null }) as never
    return new Promise(resolve => { finish = resolve }) as never
  })
  const root = node('root'), app = renderer.createApp(component())
  app.component('el-icon', { render() { return Vue.h('span', this.$slots.default?.()) } })
  app.provide(ssrContextKey, { modules: new Set() }); app.mount(root)
  await flush(); find(root, 'button', '新建 Skill').props.onClick(); await flush()
  find(root, 'button', 'analysis').props.onClick(); await flush()
  expect(all(root).find(el => el.type === 'fieldset')!.props.disabled).toBe(true)
  finish(skill); await flush()
  expect(all(root).find(el => el.type === 'fieldset')!.props.disabled).toBe(false)
  app.unmount()
})
