import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { expect, it } from 'vitest'
import GraphView from '../src/components/GraphView.vue'

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
})
