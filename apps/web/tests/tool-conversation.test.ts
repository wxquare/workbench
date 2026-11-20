import {expect, it} from 'vitest'
import source from '../src/views/Workbench.vue?raw'

it('exposes an explicit experimental tool mode with a required scope', () => {
  expect(source).toContain("mode.value === 'tool'")
  expect(source).toContain('工具对话查询范围')
  expect(source).toContain('首批仅 knowledge_search')
  expect(source).toContain('未经独立校验')
})
