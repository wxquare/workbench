import * as Vue from 'vue'
import { createRenderer, nextTick, ssrContextKey } from 'vue'
import { compile } from '@vue/compiler-dom'
import { compileScript, parse } from '@vue/compiler-sfc'
import { expect, it, vi } from 'vitest'
import Graphs from '../src/views/Graphs.vue'
import source from '../src/views/Graphs.vue?raw'
import { api, bearerToken } from '../src/session'

vi.mock('../src/components/GraphView.vue', () => ({ default: { render: () => null } }))
vi.mock('../src/session', async () => {
  const { ref } = await import('vue')
  return { bearerToken: ref(''), api: { request: vi.fn() } }
})
const route = Vue.reactive({ params: {} as Record<string, string> })
vi.mock('vue-router', () => ({ useRoute: () => route }))
interface Node { type: string; text: string; children: Node[]; parent: Node | null; props: Record<string, any> }
const node = (type: string, text = ''): Node => ({ type, text, children: [], parent: null, props: {} })
const renderer = createRenderer<Node, Node>({
  createElement: type => node(type), createText: text => node('#text', text), createComment: text => node('#comment', text),
  setText: (el, text) => { el.text = text }, setElementText: (el, text) => { el.text = text; el.children = [] },
  insert: (el, parent, anchor) => { el.parent = parent; const i = anchor ? parent.children.indexOf(anchor) : -1; if (i < 0) parent.children.push(el); else parent.children.splice(i, 0, el) },
  remove: el => { if (el.parent) el.parent.children.splice(el.parent.children.indexOf(el), 1) },
  parentNode: el => el.parent, nextSibling: el => el.parent?.children[el.parent.children.indexOf(el) + 1] ?? null,
  patchProp: (el, key, _old, value) => { el.props[key] = value },
})
const text = (el: Node): string => el.text + el.children.map(text).join('')
const flush = async () => { for (let i = 0; i < 7; i++) { await Promise.resolve(); await nextTick() } }
function component() {
  const descriptor = parse(source).descriptor
  const bindings = compileScript(descriptor, { id: 'graphs-test' }).bindings
  const code = compile(descriptor.template!.content, { mode: 'function', prefixIdentifiers: true, bindingMetadata: bindings }).code
  return { ...Graphs, render: new Function('Vue', code)(Vue) }
}
const caps = { modes: ['qa'], entrypoints: ['manual'], uses_tools: false, uses_history: false,
  needs_knowledge_tool: false, needs_workflow: false, may_interrupt: false, verified_answers: false }
const items = ['对话（单轮）', '对话（多轮）', '知识摘录', 'SOP 巡检', '合成告警诊断', '合成告警提案', '合成告警批量提案', '知识引用审查']
  .map((title, index) => ({ graph_id: index === 7 ? 'knowledge.citation_review' : `id${index}`, title, summary: `${title}简介`, status: index < 2 ? 'recommended' : 'experimental',
    agents_using: index, capabilities: { ...caps, entrypoints: index >= 4 && index <= 6 ? ['synthetic_alert'] : ['manual'] } }))

it('renders all graphs and local synthetic entry limits', async () => {
  bearerToken.value = 'token'; route.params = {}
  vi.mocked(api.request).mockResolvedValue({ items })
  const root = node('root'), app = renderer.createApp(component())
  app.component('router-link', { template: '<a><slot /></a>' })
  app.provide(ssrContextKey, { modules: new Set() }); app.mount(root); await flush()
  for (const item of items) expect(text(root)).toContain(item.title)
  expect(text(root)).toContain('工作流')
  expect(text(root).indexOf('推荐')).toBeLessThan(text(root).indexOf('能力对比'))
  expect(text(root).match(/查看详情/g)).toHaveLength(8)
  expect(text(root)).toContain('仅合成告警入口')
  expect(text(root)).toContain('本地集成测试')
  expect(text(root)).toContain('真实知识源和范围隔离尚未验收')
  expect(text(root).match(/Worker 需显式启用/g)).toHaveLength(6)
  expect(text(root)).toContain('7 个 Agent 使用')
  app.unmount()
})

it('clears the previous detail while a different graph loads', async () => {
  bearerToken.value = 'token'; route.params = { id: 'first' }
  let resolve!: (value: any) => void
  const pending = new Promise<any>(r => { resolve = r })
  const detail = { ...items[0], when_to_use: '适用', when_not_to_use: '不适用', notes: [], steps_text: ['步骤'], nodes: [], edges: [] }
  vi.mocked(api.request).mockImplementation(async path => path.endsWith('/first') ? detail : pending)
  const root = node('root'), app = renderer.createApp(component())
  app.component('router-link', { template: '<a><slot /></a>' })
  app.provide(ssrContextKey, { modules: new Set() }); app.mount(root); await flush()
  expect(text(root)).toContain(detail.title)
  expect(text(root)).toContain('执行结构')
  expect(text(root).indexOf('执行结构')).toBeLessThan(text(root).indexOf('适用场景'))
  route.params = { id: 'second' }; await flush()
  expect(text(root)).not.toContain(detail.title)
  resolve({ ...detail, title: '第二张图' }); await flush()
  expect(text(root)).toContain('第二张图')
  app.unmount()
})
