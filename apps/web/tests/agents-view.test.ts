import * as Vue from 'vue'
import { createSSRApp, createRenderer, nextTick, ssrContextKey } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { compile } from '@vue/compiler-dom'
import { compileScript, parse } from '@vue/compiler-sfc'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../src/api'
import Agents from '../src/views/Agents.vue'
import source from '../src/views/Agents.vue?raw'
import { api, bearerToken } from '../src/session'

vi.mock('../src/session', async () => {
  const { ref } = await import('vue')
  return { bearerToken: ref(''), api: { agents: vi.fn(), request: vi.fn() } }
})

interface Node {
  type: string; text: string; children: Node[]; parent: Node | null
  props: Record<string, any>; listeners: Record<string, Function>; value: string
  addEventListener(event: string, listener: Function): void
  getRootNode(): object
}
function node(type: string, text = ''): Node {
  const result: Node = { type, text, children: [], parent: null, props: {}, listeners: {}, value: '',
    getRootNode() { return {} },
    addEventListener(event, listener) { result.listeners[event] = listener } }
  return result
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
function contents(el: Node): string { return el.text + el.children.map(contents).join('') }
function find(root: Node, type: string, label: string): Node {
  const found = all(root).find(el => el.type === type && contents(el).includes(label))
  if (!found) throw new Error(`missing ${type}: ${label}`)
  return found
}
function field(root: Node, label: string, type: string): Node {
  const container = find(root, 'label', label)
  const result = all(container).find(el => el.type === type)
  if (!result) throw new Error(`missing ${type} in ${label}`)
  return result
}
async function flush() { for (let i = 0; i < 5; i++) { await Promise.resolve(); await nextTick() } }
function clientComponent() {
  const descriptor = parse(source).descriptor
  const bindings = compileScript(descriptor, { id: 'agent-test' }).bindings
  const code = compile(descriptor.template!.content, { mode: 'function', prefixIdentifiers: true, bindingMetadata: bindings }).code
  return { ...Agents, render: new Function('Vue', code)(Vue) }
}
const draft = { default_model: { resource_id: 'model', version: '1', content_hash: 'a'.repeat(64) }, graph_version: 'dod.v3', skills: [] }
const detail = { agent_id: 'ws', name: 'Operations', revision: 3, active_profile_id: 'profile', draft }

beforeEach(() => {
  vi.stubGlobal('Document', class {})
  vi.stubGlobal('ShadowRoot', class {})
  vi.mocked(api.agents).mockReset()
  vi.mocked(api.request).mockReset()
})

describe('Agent management', () => {
  it('keeps skill order when changing a version', async () => {
    bearerToken.value = 'token'
    const old = { resource_id: 'old', version: '1', content_hash: 'b'.repeat(64) }
    const newer = { resource_id: 'new', version: '2', content_hash: 'c'.repeat(64) }
    const other = { resource_id: 'other', version: '1', content_hash: 'd'.repeat(64) }
    vi.mocked(api.agents).mockResolvedValue([{ agent_id: 'ws', name: 'Operations' }])
    vi.mocked(api.request).mockImplementation(async (path, init) => {
      if (path.startsWith('/v1/skills?')) return { items: [
        { skill_id: 'one', name: 'analysis', description: 'Analysis', versions: [newer, old], latest_version: newer },
        { skill_id: 'two', name: 'other-skill', description: 'Other', versions: [other], latest_version: other },
      ], next_cursor: null } as never
      if (path.includes('/model-profiles')) return { items: [], next_cursor: null } as never
      if (init?.method === 'PATCH') return { revision: 4, draft: JSON.parse(init.body as string) } as never
      return { ...detail, draft: { ...draft, skills: [old, other] } } as never
    })
    vi.stubGlobal('crypto', { randomUUID: () => 'test-key' })
    const root = node('root'), app = renderer.createApp(clientComponent())
    app.provide(ssrContextKey, { modules: new Set() }); app.mount(root)
    await flush(); find(root, 'button', 'Operations').props.onClick(); await flush()
    field(root, 'analysis', 'select').props.onChange({ target: { value: JSON.stringify(['new', '2', 'c'.repeat(64)]) } })
    await flush(); find(root, 'button', '保存草稿').props.onClick(); await flush()
    const patch = vi.mocked(api.request).mock.calls.find(([, init]) => init?.method === 'PATCH')!
    expect(JSON.parse(patch[1]!.body as string).skills).toEqual([newer, other])
    app.unmount()
  })
  it('binds a shared skill version in the draft without overwriting other settings', async () => {
    bearerToken.value = 'token'
    const version = { resource_id: 'skill-version', version: '1', content_hash: 'b'.repeat(64) }
    vi.mocked(api.agents).mockResolvedValue([{ agent_id: 'ws', name: 'Operations' }])
    vi.mocked(api.request).mockImplementation(async (path, init) => {
      if (path.startsWith('/v1/skills?')) return { items: [{ skill_id: 'skill', name: 'analysis', description: 'Analysis', versions: [version], latest_version: version }], next_cursor: null } as never
      if (path.includes('/model-profiles')) return { items: [], next_cursor: null } as never
      if (init?.method === 'PATCH') return { revision: 4, draft: JSON.parse(init.body as string) } as never
      return { ...detail, draft: { ...draft, system_prompt: 'Keep this.' } } as never
    })
    vi.stubGlobal('crypto', { randomUUID: () => 'test-key' })
    const root = node('root'), app = renderer.createApp(clientComponent())
    app.provide(ssrContextKey, { modules: new Set() }); app.mount(root)
    await flush(); find(root, 'button', 'Operations').props.onClick(); await flush()
    expect(contents(root)).not.toContain('analysis')
    find(root, 'button', '添加 Skill').props.onClick(); await flush()
    const checkbox = field(root, 'analysis', 'input')
    checkbox.props.onChange({ target: { checked: true } }); await flush()
    find(root, 'button', '添加所选').props.onClick(); await flush()
    await flush(); find(root, 'button', '保存草稿').props.onClick(); await flush()
    const patch = vi.mocked(api.request).mock.calls.find(([, init]) => init?.method === 'PATCH')!
    expect(JSON.parse(patch[1]!.body as string)).toMatchObject({ skills: [version], system_prompt: 'Keep this.' })
    app.unmount()
  })
  it('filters skills, does not add on cancel, and can remove a binding', async () => {
    bearerToken.value = 'token'
    const one = { resource_id: 'one-v1', version: '1', content_hash: 'b'.repeat(64) }
    const two = { resource_id: 'two-v1', version: '1', content_hash: 'c'.repeat(64) }
    vi.mocked(api.agents).mockResolvedValue([{ agent_id: 'ws', name: 'Operations' }])
    vi.mocked(api.request).mockImplementation(async (path, init) => {
      if (path.startsWith('/v1/skills?')) return { items: [
        { skill_id: 'one', name: 'analysis', description: 'Analysis', versions: [one], latest_version: one },
        { skill_id: 'two', name: 'report', description: 'Reports', versions: [two], latest_version: two },
        { skill_id: 'three', name: 'unpublished', description: 'Draft', versions: [], latest_version: null },
      ], next_cursor: null } as never
      if (path.includes('/model-profiles')) return { items: [], next_cursor: null } as never
      if (init?.method === 'PATCH') return { revision: 4, draft: JSON.parse(init.body as string) } as never
      return { ...detail, draft: { ...draft, skills: [one] } } as never
    })
    vi.stubGlobal('crypto', { randomUUID: () => 'test-key' })
    const root = node('root'), app = renderer.createApp(clientComponent())
    app.provide(ssrContextKey, { modules: new Set() }); app.mount(root)
    await flush(); find(root, 'button', 'Operations').props.onClick(); await flush()
    expect(contents(root)).toContain('analysis')
    expect(contents(root)).not.toContain('report')
    find(root, 'button', '添加 Skill').props.onClick(); await flush()
    let dialog = all(root).find(el => el.props.role === 'dialog')!
    expect(field(dialog, 'analysis', 'input').props.disabled).toBe(true)
    expect(field(dialog, 'unpublished', 'input').props.disabled).toBe(true)
    const overlay = all(root).find(el => el.props.class === 'skill-overlay')!
    overlay.props.onKeydown({ key: 'Escape', stopPropagation() {}, preventDefault() {} }); await flush()
    expect(all(root).some(el => el.props.role === 'dialog')).toBe(false)
    find(root, 'button', '添加 Skill').props.onClick(); await flush()
    dialog = all(root).find(el => el.props.role === 'dialog')!
    const search = field(dialog, '搜索 Skill', 'input')
    search.props.onInput({ target: { value: 'report' } }); await flush()
    expect(contents(root)).not.toContain('unpublished')
    field(dialog, 'report', 'input').props.onChange({ target: { checked: true } }); await flush()
    find(root, 'button', '取消').props.onClick(); await flush()
    expect(contents(root)).not.toContain('report')
    find(root, 'button', '添加 Skill').props.onClick(); await flush()
    dialog = all(root).find(el => el.props.role === 'dialog')!
    field(dialog, 'report', 'input').props.onChange({ target: { checked: true } }); await flush()
    find(root, 'button', '添加所选').props.onClick(); await flush()
    all(root).find(el => el.type === 'button' && el.props['aria-label'] === '移除 analysis')!.props.onClick(); await flush()
    find(root, 'button', '保存草稿').props.onClick(); await flush()
    const patch = vi.mocked(api.request).mock.calls.find(([, init]) => init?.method === 'PATCH')!
    expect(JSON.parse(patch[1]!.body as string).skills).toEqual([two])
    app.unmount()
  })
  it('edits two independently configured agents without carrying settings between them', async () => {
    bearerToken.value = 'token'
    const dodModel = { resource_id: 'dod-model', version: '1', content_hash: 'a'.repeat(64) }
    const writingModel = { resource_id: 'writing-model', version: '2', content_hash: 'b'.repeat(64) }
    const details = [
      { agent_id: 'dod-agent', name: 'DoD Helper', revision: 3, active_profile_id: 'dod-profile',
        draft: { ...draft, graph_version: 'dod.v17', system_prompt: 'Investigate incidents.', default_model: dodModel } },
      { agent_id: 'writing-agent', name: 'Writing Helper', revision: 8, active_profile_id: 'writing-profile',
        draft: { ...draft, graph_version: 'dod.v18', system_prompt: 'Improve writing.', default_model: writingModel } },
    ]
    vi.mocked(api.agents).mockResolvedValue(details)
    vi.mocked(api.request).mockImplementation(async (path, init) => {
      const item = details.find(item => path.startsWith(`/v1/agents/${item.agent_id}`))
      if (!item) throw new Error(`Unexpected path: ${path}`)
      if (path.endsWith('/model-profiles?limit=100')) return { items: [
        { name: 'DoD Model', active_ref: dodModel }, { name: 'Writing Model', active_ref: writingModel },
      ], next_cursor: null } as never
      if (init?.method === 'PATCH') {
        item.draft = JSON.parse(init.body as string)
        item.revision++
      }
      return structuredClone(item) as never
    })
    vi.stubGlobal('crypto', { randomUUID: () => 'test-key' })
    const root = node('root'), app = renderer.createApp(clientComponent())
    app.provide(ssrContextKey, { modules: new Set() }); app.mount(root)
    await flush()
    find(root, 'button', 'DoD Helper').props.onClick(); await flush()
    expect(field(root, '系统提示词', 'textarea').props.value).toBe('Investigate incidents.')
    expect(field(root, '工作流', 'select').props.value).toBe('dod.v17')
    expect(field(root, '默认模型', 'select').props.value).toBe(JSON.stringify(['dod-model', '1', 'a'.repeat(64)]))
    field(root, '系统提示词', 'textarea').props.onInput({ target: { value: 'Investigate service incidents.' } })
    await flush(); find(root, 'button', '保存草稿').props.onClick(); await flush()

    find(root, 'button', 'Writing Helper').props.onClick(); await flush()
    expect(field(root, '系统提示词', 'textarea').props.value).toBe('Improve writing.')
    expect(field(root, '工作流', 'select').props.value).toBe('dod.v18')
    expect(field(root, '默认模型', 'select').props.value).toBe(JSON.stringify(['writing-model', '2', 'b'.repeat(64)]))
    field(root, '系统提示词', 'textarea').props.onInput({ target: { value: 'Edit technical writing.' } })
    await flush(); find(root, 'button', '保存草稿').props.onClick(); await flush()

    find(root, 'button', 'DoD Helper').props.onClick(); await flush()
    expect(field(root, '系统提示词', 'textarea').props.value).toBe('Investigate service incidents.')
    expect(field(root, '工作流', 'select').props.value).toBe('dod.v17')
    expect(field(root, '默认模型', 'select').props.value).toBe(JSON.stringify(['dod-model', '1', 'a'.repeat(64)]))
    const saves = vi.mocked(api.request).mock.calls.filter(([, init]) => init?.method === 'PATCH')
    expect(saves.map(([path, init]) => [path, new Headers(init?.headers).get('If-Match'), JSON.parse(init!.body as string)]))
      .toEqual([
        ['/v1/agents/dod-agent', '3', { ...draft, graph_version: 'dod.v17', system_prompt: 'Investigate service incidents.', default_model: dodModel }],
        ['/v1/agents/writing-agent', '8', { ...draft, graph_version: 'dod.v18', system_prompt: 'Edit technical writing.', default_model: writingModel }],
      ])
    app.unmount()
  })

  it('requires a bearer token before loading or creating agents', async () => {
    bearerToken.value = ''
    const html = await renderToString(createSSRApp(Agents))
    expect(html).toContain('Agent 管理')
    expect(html).toContain('新建 Agent')
    expect(html).toContain('Agent 列表')
    expect(html).toContain('请先提供 Bearer Token')
    expect(html).toContain('disabled')
    expect(api.agents).not.toHaveBeenCalled()
  })

  it('preserves edits on revision conflict and asks before discarding them', async () => {
    bearerToken.value = 'token'
    vi.mocked(api.agents).mockResolvedValue([{ agent_id: 'ws', name: 'Operations' }])
    vi.mocked(api.request).mockImplementation(async (path, init) => {
      if (path.includes('/model-profiles')) return { items: [], next_cursor: null } as never
      if (init?.method === 'PATCH') throw new ApiError(409, 'REVISION_CONFLICT')
      return detail as never
    })
    vi.stubGlobal('window', { confirm: vi.fn(() => false) })
    vi.stubGlobal('crypto', { randomUUID: () => 'test-key' })
    const root = node('root')
    const app = renderer.createApp(clientComponent())
    app.provide(ssrContextKey, { modules: new Set() })
    app.mount(root)
    await flush()
    find(root, 'button', 'Operations').props.onClick()
    await flush()
    const textarea = field(root, '配置草稿 JSON', 'textarea')
    textarea.value = JSON.stringify({ ...draft, graph_version: 'dod.v4' })
    textarea.listeners.input({ target: textarea })
    await flush()
    find(root, 'button', '保存草稿').props.onClick()
    await flush()
    expect(contents(root)).toContain('版本冲突')
    expect(find(root, 'button', '保存草稿').props.disabled).toBe(true)
    expect(find(root, 'button', '发布草稿').props.disabled).toBe(true)
    expect(textarea.value).toContain('dod.v4')
    find(root, 'button', '重新加载配置').props.onClick()
    await flush()
    expect(textarea.value).toContain('dod.v4')
    expect(vi.mocked(window.confirm)).toHaveBeenCalled()
    app.unmount()
  })

  it('changes the workflow without replacing other draft fields and confirms publication', async () => {
    bearerToken.value = 'token'
    vi.mocked(api.agents).mockResolvedValue([{ agent_id: 'ws', name: 'Operations' }])
    vi.mocked(api.request).mockImplementation(async (path, init) => {
      if (path.includes('/model-profiles')) return { items: [], next_cursor: null } as never
      if (init?.method === 'PATCH') return { agent_id: 'ws', revision: 4, draft: JSON.parse(init.body as string) } as never
      if (init?.method === 'POST') return { profile_id: 'new', version: '2' } as never
      return detail as never
    })
    const confirm = vi.fn(() => false)
    vi.stubGlobal('window', { confirm })
    vi.stubGlobal('crypto', { randomUUID: () => 'test-key' })
    const root = node('root')
    const app = renderer.createApp(clientComponent())
    app.provide(ssrContextKey, { modules: new Set() })
    app.mount(root)
    await flush()
    find(root, 'button', 'Operations').props.onClick()
    await flush()
    const workflow = find(root, 'select', 'dod.v18')
    expect(contents(workflow)).toContain('dod.v17')
    workflow.props.onChange({ target: { value: 'dod.v18' } })
    await flush()
    find(root, 'button', '保存草稿').props.onClick()
    await flush()
    const patch = vi.mocked(api.request).mock.calls.find(([, init]) => init?.method === 'PATCH')!
    expect(JSON.parse(patch[1]!.body as string)).toEqual({ ...draft, graph_version: 'dod.v18' })
    find(root, 'button', '发布草稿').props.onClick()
    await flush()
    expect(confirm).toHaveBeenCalled()
    expect(vi.mocked(api.request).mock.calls.some(([, init]) => init?.method === 'POST')).toBe(false)
    confirm.mockReturnValue(true)
    find(root, 'button', '发布草稿').props.onClick()
    await flush()
    expect(vi.mocked(api.request).mock.calls.some(([, init]) => init?.method === 'POST')).toBe(true)
    app.unmount()
  })

  it('blocks further writes when publication succeeds but refreshing the revision fails', async () => {
    bearerToken.value = 'token'
    vi.mocked(api.agents).mockResolvedValue([{ agent_id: 'ws', name: 'Operations' }])
    let reads = 0
    vi.mocked(api.request).mockImplementation(async (path, init) => {
      if (path.includes('/model-profiles')) return { items: [], next_cursor: null } as never
      if (init?.method === 'POST') return { profile_id: 'new', version: '2' } as never
      if (reads++) throw new Error('offline')
      return detail as never
    })
    vi.stubGlobal('window', { confirm: () => true })
    vi.stubGlobal('crypto', { randomUUID: () => 'test-key' })
    const root = node('root')
    const app = renderer.createApp(clientComponent())
    app.provide(ssrContextKey, { modules: new Set() })
    app.mount(root)
    await flush()
    find(root, 'button', 'Operations').props.onClick()
    await flush()
    find(root, 'button', '发布草稿').props.onClick()
    await flush()
    expect(contents(root)).toContain('发布成功，但刷新失败')
    expect(find(root, 'button', '发布草稿').props.disabled).toBe(true)
    expect(find(root, 'button', '保存草稿').props.disabled).toBe(true)
    app.unmount()
  })

  it('binds only a selected real model ref and preserves the remaining draft', async () => {
    bearerToken.value = 'token'
    vi.mocked(api.agents).mockResolvedValue([{ agent_id: 'ws', name: 'Operations' }])
    const modelRef = { resource_id: 'new-model-version', version: '2', content_hash: 'c'.repeat(64) }
    vi.mocked(api.request).mockImplementation(async (path, init) => {
      if (path.includes('/model-profiles')) return { items: [{ name: 'Real model', active_ref: modelRef }], next_cursor: null } as never
      if (init?.method === 'PATCH') return { agent_id: 'ws', revision: 4, draft: JSON.parse(init.body as string) } as never
      return detail as never
    })
    vi.stubGlobal('window', { confirm: () => true })
    vi.stubGlobal('crypto', { randomUUID: () => 'test-key' })
    const root = node('root')
    const app = renderer.createApp(clientComponent())
    app.provide(ssrContextKey, { modules: new Set() })
    app.mount(root)
    await flush()
    find(root, 'button', 'Operations').props.onClick()
    await flush()
    const select = find(root, 'select', 'Real model')
    const option = find(select, 'option', 'Real model')
    select.props.onChange({ target: { value: option.props.value } })
    await flush()
    find(root, 'button', '保存草稿').props.onClick()
    await flush()
    const patch = vi.mocked(api.request).mock.calls.find(([, init]) => init?.method === 'PATCH')!
    expect(JSON.parse(patch[1]!.body as string)).toEqual({ ...draft, default_model: modelRef })
    app.unmount()
  })

  it('keeps the existing model ref and reports a model-list permission failure', async () => {
    bearerToken.value = 'token'
    vi.mocked(api.agents).mockResolvedValue([{ agent_id: 'ws', name: 'Operations' }])
    vi.mocked(api.request).mockImplementation(async (path) => {
      if (path.includes('/model-profiles')) throw new ApiError(403, 'OPERATION_DENIED')
      return detail as never
    })
    const root = node('root')
    const app = renderer.createApp(clientComponent())
    app.provide(ssrContextKey, { modules: new Set() })
    app.mount(root)
    await flush()
    find(root, 'button', 'Operations').props.onClick()
    await flush()
    expect(contents(root)).toContain('权限不足')
    expect(find(root, 'select', '当前模型').props.disabled).toBe(true)
    expect(field(root, '配置草稿 JSON', 'textarea').value).toContain('"resource_id": "model"')
    app.unmount()
  })

  it('saves structured instructions and explicit budget clears while preserving the draft', async () => {
    bearerToken.value = 'token'
    vi.mocked(api.agents).mockResolvedValue([{ agent_id: 'ws', name: 'Operations' }])
    const original = { ...draft, graph_version: 'dod.v18', description: 'old', system_prompt: 'old prompt',
      budget_limits: { tokens: 500, tool_calls: 10 }, skills: [{ resource_id: 'existing' }] }
    vi.mocked(api.request).mockImplementation(async (path, init) => {
      if (path.includes('/model-profiles')) return { items: [], next_cursor: null } as never
      if (init?.method === 'PATCH') return { agent_id: 'ws', revision: 4, draft: JSON.parse(init.body as string) } as never
      return { ...detail, draft: original } as never
    })
    vi.stubGlobal('crypto', { randomUUID: () => 'test-key' })
    const root = node('root'), app = renderer.createApp(clientComponent())
    app.provide(ssrContextKey, { modules: new Set() }); app.mount(root)
    await flush(); find(root, 'button', 'Operations').props.onClick(); await flush()
    expect(field(root, '回复语言', 'select').props.value).toBe('auto')
    expect(field(root, '回复格式', 'select').props.value).toBe('default')
    expect(contents(root)).toContain('上下文管理')
    expect(contents(root)).toContain('不会删除会话记录')
    expect(field(root, '对话历史轮数', 'input').props.value).toBe(8)
    field(root, '描述', 'textarea').props.onInput({ target: { value: '' } })
    field(root, '系统提示词', 'textarea').props.onInput({ target: { value: 'Be concise.' } })
    field(root, '回复语言', 'select').props.onChange({ target: { value: 'en' } })
    field(root, '回复格式', 'select').props.onChange({ target: { value: 'json' } })
    field(root, '对话历史轮数', 'input').props.onInput({ target: { value: '0' } })
    field(root, 'Token 上限', 'input').props.onInput({ target: { value: '' } })
    field(root, '模型调用上限', 'input').props.onInput({ target: { value: '4' } })
    await flush()
    expect(find(root, 'button', '发布草稿').props.disabled).toBe(true)
    find(root, 'button', '保存草稿').props.onClick(); await flush()
    const patch = vi.mocked(api.request).mock.calls.find(([, init]) => init?.method === 'PATCH')!
    expect(JSON.parse(patch[1]!.body as string)).toEqual({ ...original, description: '', system_prompt: 'Be concise.',
      response_language: 'en', response_format: 'json', conversation_history_turns: 0,
      budget_limits: { tokens: null, tool_calls: 10, model_calls: 4 } })
    expect(find(root, 'button', '发布草稿').props.disabled).toBe(false)
    app.unmount()
  })

  it('prevents invalid structured values from being saved and limits history editing to v18', async () => {
    bearerToken.value = 'token'
    vi.mocked(api.agents).mockResolvedValue([{ agent_id: 'ws', name: 'Operations' }])
    vi.mocked(api.request).mockImplementation(async path => path.includes('/model-profiles')
      ? { items: [], next_cursor: null } as never : { ...detail, draft: { ...draft, graph_version: 'dod.v17' } } as never)
    const root = node('root'), app = renderer.createApp(clientComponent())
    app.provide(ssrContextKey, { modules: new Set() }); app.mount(root)
    await flush(); find(root, 'button', 'Operations').props.onClick(); await flush()
    expect(field(root, '对话历史轮数', 'input').props.disabled).toBe(true)
    find(root, 'select', 'dod.v18').props.onChange({ target: { value: 'dod.v18' } }); await flush()
    expect(field(root, '对话历史轮数', 'input').props.disabled).toBe(false)
    field(root, '模型调用上限', 'input').props.onInput({ target: { value: '0' } }); await flush()
    expect(find(root, 'button', '保存草稿').props.disabled).toBe(true)
    expect(vi.mocked(api.request).mock.calls.some(([, init]) => init?.method === 'PATCH')).toBe(false)
    field(root, '模型调用上限', 'input').props.onInput({ target: { value: '5' } }); await flush()
    expect(find(root, 'button', '保存草稿').props.disabled).toBe(false)
    field(root, '对话历史轮数', 'input').props.onInput({ target: { value: '9' } }); await flush()
    expect(find(root, 'button', '保存草稿').props.disabled).toBe(true)
    bearerToken.value = 'other-token'; await flush()
    expect(contents(root)).not.toContain('对话历史轮数')
    app.unmount()
  })

  it('reflects JSON edits in the controls and blocks invalid values until corrected', async () => {
    bearerToken.value = 'token'
    vi.mocked(api.agents).mockResolvedValue([{ agent_id: 'ws', name: 'Operations' }])
    vi.mocked(api.request).mockImplementation(async path => path.includes('/model-profiles')
      ? { items: [], next_cursor: null } as never : detail as never)
    const root = node('root'), app = renderer.createApp(clientComponent())
    app.provide(ssrContextKey, { modules: new Set() }); app.mount(root)
    await flush(); find(root, 'button', 'Operations').props.onClick(); await flush()
    const json = field(root, '配置草稿 JSON', 'textarea')
    json.value = JSON.stringify({ ...draft, description: 'x'.repeat(1001), response_language: 'zh', budget_limits: { tokens: -1 } })
    json.listeners.input({ target: json }); await flush()
    expect(field(root, '回复语言', 'select').props.value).toBe('zh')
    expect(find(root, 'button', '保存草稿').props.disabled).toBe(true)
    json.value = JSON.stringify({ ...draft, description: 'valid', response_language: 'zh', budget_limits: { tokens: 50 } })
    json.listeners.input({ target: json }); await flush()
    expect(field(root, '描述', 'textarea').props.value).toBe('valid')
    expect(field(root, 'Token 上限', 'input').props.value).toBe(50)
    expect(find(root, 'button', '保存草稿').props.disabled).toBe(false)
    json.value = JSON.stringify({ ...draft, response_format: null })
    json.listeners.input({ target: json }); await flush()
    expect(find(root, 'button', '保存草稿').props.disabled).toBe(true)
    app.unmount()
  })

  it('asks before leaving for model management with an unsaved draft', async () => {
    bearerToken.value = 'token'
    vi.mocked(api.agents).mockResolvedValue([{ agent_id: 'ws', name: 'Operations' }])
    vi.mocked(api.request).mockImplementation(async path => path.includes('/model-profiles')
      ? { items: [], next_cursor: null } as never : detail as never)
    vi.stubGlobal('window', { confirm: vi.fn(() => false) })
    const root = node('root'), app = renderer.createApp(clientComponent())
    app.provide(ssrContextKey, { modules: new Set() }); app.mount(root)
    await flush(); find(root, 'button', 'Operations').props.onClick(); await flush()
    field(root, '描述', 'textarea').props.onInput({ target: { value: 'unsaved' } }); await flush()
    const preventDefault = vi.fn()
    find(root, 'a', '模型管理').props.onClick({ preventDefault })
    expect(preventDefault).toHaveBeenCalled()
    expect(vi.mocked(window.confirm)).toHaveBeenCalled()
    app.unmount()
  })
})
