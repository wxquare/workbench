import * as Vue from 'vue'
import { createRenderer, nextTick, ssrContextKey } from 'vue'
import { compile } from '@vue/compiler-dom'
import { compileScript, parse } from '@vue/compiler-sfc'
import { expect, it, vi } from 'vitest'
import Skills from '../src/views/Skills.vue'
import source from '../src/views/Skills.vue?raw'
import SkillFilesEditor from '../src/components/SkillFilesEditor.vue'
import editorSource from '../src/components/SkillFilesEditor.vue?raw'
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
  const editorDescriptor = parse(editorSource).descriptor
  const editorBindings = compileScript(editorDescriptor, { id: 'files-editor-test' }).bindings
  const editorCode = compile(editorDescriptor.template!.content, { mode: 'function', prefixIdentifiers: true, bindingMetadata: editorBindings }).code
  Object.assign(SkillFilesEditor, { render: new Function('Vue', editorCode)(Vue) })
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
    input.value = value
    if (input.listeners.input) input.listeners.input({ target: input })
    else input.props.onInput({ target: input })
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

it('edits a package without losing text when switching, renaming or removing attachments', async () => {
  vi.stubGlobal('Document', class {})
  vi.stubGlobal('ShadowRoot', class {})
  vi.stubGlobal('crypto', { randomUUID: () => 'file-key' })
  vi.stubGlobal('window', { confirm: () => true })
  bearerToken.value = 'token'
  let saved: Record<string, any> | undefined
  vi.mocked(api.request).mockImplementation(async (path, init) => {
    if (path.startsWith('/v1/skills?')) return { items: [], next_cursor: null } as never
    if (init?.method === 'POST') {
      saved = JSON.parse(init.body as string)
      return { skill_id: 'multi', name: 'analysis', revision: 0, versions: [], latest_version: null, draft: saved } as never
    }
    throw new Error('unexpected request')
  })
  const root = node('root'), app = renderer.createApp(component())
  app.provide(ssrContextKey, { modules: new Set() }); app.mount(root)
  await flush(); find(root, 'button', '新建 Skill').props.onClick(); await flush()
  function input(label: string, value: string) {
    const field = all(find(root, 'label', label)).find(el => el.type === 'input' || el.type === 'textarea')!
    field.value = value
    if (field.listeners.input) field.listeners.input({ target: field })
    else field.props.onInput({ target: field })
  }
  input('技能标识', 'analysis'); input('描述', 'Analyse'); input('技能指令', 'Main instruction')
  input('文件路径', 'scripts/analyse.py'); await flush()
  find(root, 'button', '新建文件').props.onClick(); await flush()
  input('文件内容', 'print("evidence")'); await flush()
  input('文件路径', 'scripts/analyse.py'); await flush()
  find(root, 'button', '重命名文件').props.onClick(); await flush()
  expect(all(root).some(el => el.type === 'button' && text(el) === 'scripts/analyse.py')).toBe(true)
  find(root, 'button', 'SKILL.md').props.onClick(); await flush()
  expect(all(find(root, 'label', '技能指令')).find(el => el.type === 'textarea')!.props.value).toBe('Main instruction')
  find(root, 'button', 'scripts/analyse.py').props.onClick(); await flush()
  input('文件路径', 'scripts/report.py'); await flush()
  find(root, 'button', '重命名文件').props.onClick(); await flush()
  find(root, 'button', '保存草稿').props.onClick(); await flush()
  expect(saved?.files).toEqual({
    'SKILL.md': { encoding: 'utf-8', content: 'Main instruction' },
    'scripts/report.py': { encoding: 'utf-8', content: 'print("evidence")' },
  })
  find(root, 'button', 'SKILL.md').props.onClick(); await flush()
  expect(find(root, 'button', '删除文件').props.disabled).toBe(true)
  find(root, 'button', 'scripts/report.py').props.onClick(); await flush()
  find(root, 'button', '删除文件').props.onClick(); await flush()
  expect(all(root).some(el => el.type === 'button' && text(el) === 'scripts/report.py')).toBe(false)
  app.unmount()
})

it('uploads and replaces an image, saves Base64 and browses historical resources read-only', async () => {
  vi.stubGlobal('Document', class {})
  vi.stubGlobal('ShadowRoot', class {})
  vi.stubGlobal('crypto', { randomUUID: () => 'image-key' })
  vi.stubGlobal('window', { confirm: () => true })
  bearerToken.value = 'token'
  const bytes = new Uint8Array([137,80,78,71,13,10,26,10,1])
  let saved: Record<string, any> | undefined
  const skill = { skill_id: 'image', name: 'analysis', description: 'Analyse', revision: 0, updated_at: 1,
    versions: [{ resource_id: 'v1', version: '1', content_hash: 'a'.repeat(64) }], latest_version: null,
    draft: { description: 'Analyse', content: 'Main' } }
  vi.mocked(api.request).mockImplementation(async (path, init) => {
    if (path.startsWith('/v1/skills?')) return { items: [skill], next_cursor: null } as never
    if (path.endsWith('/versions/1')) return { content: 'Published main', files: {
      'SKILL.md': { encoding: 'utf-8', content: 'Published main' },
      'scripts/old.py': { encoding: 'utf-8', content: 'print("old")' },
    } } as never
    if (init?.method === 'PATCH') {
      saved = JSON.parse(init.body as string)
      return { ...skill, draft: saved, revision: 1 } as never
    }
    return skill as never
  })
  const root = node('root'), app = renderer.createApp(component())
  app.provide(ssrContextKey, { modules: new Set() }); app.mount(root)
  await flush(); find(root, 'button', 'analysis').props.onClick(); await flush()
  const pathInput = all(find(root, 'label', '文件路径')).find(el => el.type === 'input')!
  pathInput.value = 'assets/topology.png'; pathInput.listeners.input({ target: pathInput }); await flush()
  const upload = all(root).find(el => el.type === 'input' && el.props.type === 'file')!
  const uploaded = { name: 'topology.png', size: bytes.length, arrayBuffer: async () => bytes.buffer }
  await upload.props.onChange({ target: { files: [uploaded], value: 'file' } }); await flush()
  expect(all(root).find(el => el.type === 'img')!.props.src).toBe('data:image/png;base64,iVBORw0KGgoB')
  pathInput.value = 'assets/topology.png'; pathInput.listeners.input({ target: pathInput }); await flush()
  const replacement = new Uint8Array([...bytes, 2])
  await upload.props.onChange({ target: { files: [{ ...uploaded, size: replacement.length, arrayBuffer: async () => replacement.buffer }], value: 'file' } }); await flush()
  find(root, 'button', '保存草稿').props.onClick(); await flush()
  expect(saved?.files['assets/topology.png']).toEqual({ encoding: 'base64', content: 'iVBORw0KGgoBAg==' })
  find(root, 'button', '版本 1').props.onClick(); await flush()
  const history = all(root).find(el => el.props['aria-label'] === '历史版本文件')!
  find(history, 'button', 'scripts/old.py').props.onClick(); await flush()
  const content = all(history).find(el => el.type === 'textarea')!
  expect(content.props.value).toBe('print("old")')
  expect(content.props.readonly).toBe(true)
  expect(all(history).some(el => el.type === 'input')).toBe(false)
  app.unmount()
})

it('ignores a file read that finishes after switching to another Skill', async () => {
  vi.stubGlobal('Document', class {})
  vi.stubGlobal('ShadowRoot', class {})
  vi.stubGlobal('window', { confirm: () => true })
  bearerToken.value = 'token'
  const first = { skill_id: 'first', name: 'first', description: 'First', revision: 0, versions: [], latest_version: null, draft: { description: 'First', content: 'First main' } }
  const second = { ...first, skill_id: 'second', name: 'second', draft: { description: 'Second', content: 'Second main' } }
  vi.mocked(api.request).mockImplementation(async path => {
    if (path.startsWith('/v1/skills?')) return { items: [first, second], next_cursor: null } as never
    return path.endsWith('/second') ? second as never : first as never
  })
  const root = node('root'), app = renderer.createApp(component())
  app.provide(ssrContextKey, { modules: new Set() }); app.mount(root)
  await flush(); find(root, 'button', 'first').props.onClick(); await flush()
  let finish!: (value: ArrayBuffer) => void
  const pending = new Promise<ArrayBuffer>(resolve => { finish = resolve })
  const upload = all(root).find(el => el.type === 'input' && el.props.type === 'file')!
  const reading = upload.props.onChange({ target: { files: [{ name: 'late.png', size: 8, arrayBuffer: () => pending }], value: 'file' } })
  await flush(); find(root, 'button', 'second').props.onClick(); await flush()
  finish(new Uint8Array([137,80,78,71,13,10,26,10]).buffer)
  await reading; await flush()
  const main = all(find(root, 'label', '技能指令')).find(el => el.type === 'textarea')!
  expect(main.props.value).toBe('Second main')
  expect(all(root).some(el => el.type === 'img')).toBe(false)
  main.props.onInput({ target: { value: 'Changed second main' } }); await flush()
  expect(find(root, 'button', '保存草稿').props.disabled).toBe(false)
  app.unmount()
})

it('keeps the most recently selected historical version when requests finish out of order', async () => {
  vi.stubGlobal('Document', class {})
  vi.stubGlobal('ShadowRoot', class {})
  vi.stubGlobal('window', { confirm: () => true })
  bearerToken.value = 'token'
  const skill = { skill_id: 'history', name: 'history', description: 'History', revision: 0, latest_version: null,
    versions: [{ resource_id: 'v1', version: '1', content_hash: 'a'.repeat(64) }, { resource_id: 'v2', version: '2', content_hash: 'b'.repeat(64) }], draft: { description: 'History', content: 'Draft' } }
  let older!: (value: unknown) => void
  vi.mocked(api.request).mockImplementation(async path => {
    if (path.startsWith('/v1/skills?')) return { items: [skill], next_cursor: null } as never
    if (path.endsWith('/versions/1')) return new Promise(resolve => { older = resolve }) as never
    if (path.endsWith('/versions/2')) return { content: 'Newer published main' } as never
    return skill as never
  })
  const root = node('root'), app = renderer.createApp(component())
  app.provide(ssrContextKey, { modules: new Set() }); app.mount(root)
  await flush(); find(root, 'button', 'history').props.onClick(); await flush()
  find(root, 'button', '版本 1').props.onClick(); await flush()
  find(root, 'button', '版本 2').props.onClick(); await flush()
  older({ content: 'Older published main' }); await flush()
  const history = all(root).find(el => el.props['aria-label'] === '历史版本文件')!
  expect(all(history).find(el => el.type === 'textarea')!.props.value).toBe('Newer published main')
  expect(all(find(root, 'label', '技能指令')).find(el => el.type === 'textarea')!.props.value).toBe('Draft')
  app.unmount()
})
