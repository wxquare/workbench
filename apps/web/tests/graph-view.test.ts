import { createRenderer, createSSRApp, nextTick, ssrContextKey } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { compile } from '@vue/compiler-dom'
import { compileScript, parse } from '@vue/compiler-sfc'
import { expect, it } from 'vitest'
import GraphView from '../src/components/GraphView.vue'
import source from '../src/components/GraphView.vue?raw'

it('shows a labelled self loop and readable node details', async () => {
  const nodes = [
    { id: 'step', title: '执行步骤', kind: 'tool', summary: '执行工具', reads: ['工作流'], writes: ['步骤结果'], cost: ['工具调用'] },
  ]
  const edges = [{ source: 'step', target: 'step', conditional: true, label: '还有步骤' }]
  const html = await renderToString(createSSRApp(GraphView, { nodes, edges }))
  expect(html).toContain('执行步骤')
  expect(html).toContain('还有步骤')
  expect(html).toContain('条件')
  expect(html).toContain('读取')
  expect(html).toContain('工具调用')
  expect(html).toContain('tabindex="0"')
  expect(html).toContain('role="group" aria-label="流程结构图"')
})

it('labels unselected node types in the diagram', async () => {
  const nodes = [
    { id: 'prepare', title: '准备工作流', kind: 'prepare', summary: '准备输入', reads: [], writes: [], cost: [] },
    { id: 'check', title: '检查证据', kind: 'validate', summary: '检查来源', reads: [], writes: [], cost: [] },
  ]
  const html = await renderToString(createSSRApp(GraphView, { nodes, edges: [] }))
  expect(html).toContain('>校验</text>')
})

it('shows the first execution node and updates the side inspector when another node is selected', async () => {
  interface VNode { type: string; text: string; children: VNode[]; parent: VNode | null; props: Record<string, any> }
  const node = (type: string, text = ''): VNode => ({ type, text, children: [], parent: null, props: {} })
  const renderer = createRenderer<VNode, VNode>({
    createElement: type => node(type), createText: text => node('#text', text), createComment: text => node('#comment', text),
    setText: (el, value) => { el.text = value }, setElementText: (el, value) => { el.text = value; el.children = [] },
    insert: (el, parent, anchor) => { el.parent = parent; const i = anchor ? parent.children.indexOf(anchor) : -1; if (i < 0) parent.children.push(el); else parent.children.splice(i, 0, el) },
    remove: el => { if (el.parent) el.parent.children.splice(el.parent.children.indexOf(el), 1) },
    parentNode: el => el.parent, nextSibling: el => el.parent?.children[el.parent.children.indexOf(el) + 1] ?? null,
    patchProp: (el, key, _old, value) => { el.props[key] = value },
  })
  const all = (root: VNode): VNode[] => [root, ...root.children.flatMap(all)]
  const visibleText = (root: VNode): string => all(root).map(n => n.text).join(' ')
  const nodes = [
    { id: '__start__', title: '开始', kind: 'finish', summary: '起点', reads: [], writes: [], cost: [] },
    { id: 'answer', title: '生成回答', kind: 'model', summary: '结合历史回答', reads: ['问题'], writes: ['回答'], cost: ['模型调用'] },
    { id: 'check', title: '校验证据', kind: 'validate', summary: '检查引用', reads: ['来源'], writes: ['校验结果'], cost: [] },
  ]
  const descriptor = parse(source).descriptor
  const bindings = compileScript(descriptor, { id: 'graph-view-test' }).bindings
  const code = compile(descriptor.template!.content, { mode: 'function', prefixIdentifiers: true, bindingMetadata: bindings }).code
  const component = { ...GraphView, render: new Function('Vue', code)(await import('vue')) }
  const root = node('root'), app = renderer.createApp(component, { nodes, edges: [{ source: 'answer', target: 'check', conditional: false, label: '' }] })
  app.provide(ssrContextKey, { modules: new Set() })
  app.mount(root)
  const inspector = () => all(root).find(n => n.props['aria-label'] === '节点详情')!
  expect(inspector()).toBeDefined()
  expect(visibleText(inspector())).toContain('结合历史回答')
  expect(visibleText(inspector())).toContain('模型调用')
  all(root).find(n => n.props['aria-label'] === '校验证据' && n.type === 'g')!.props.onClick()
  await nextTick()
  expect(visibleText(inspector())).toContain('检查引用')
  expect(visibleText(inspector())).toContain('来源')
  expect(visibleText(inspector())).not.toContain('结合历史回答')
  app.unmount()
})
