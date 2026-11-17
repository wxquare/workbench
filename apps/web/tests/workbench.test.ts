import { createSSRApp, ref } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createMemoryHistory, createRouter } from 'vue-router'
import { describe, expect, it, vi } from 'vitest'
import Workbench from '../src/views/Workbench.vue'

vi.mock('../src/session', () => ({
  bearerToken: ref(''),
  api: {},
}))

describe('QA workbench', () => {
  it('offers both modes and starts without knowledge scope fields', async () => {
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: Workbench }] })
    const app = createSSRApp(Workbench).use(router)
    const html = await renderToString(app)
    expect(html).toContain('请选择 Agent')
    expect(html).toContain('纯 LLM 问答')
    expect(html).toContain('知识问答')
    expect(html).not.toContain('查询范围')
    expect(html).not.toContain('服务名称')
    expect(html).not.toContain('datetime-local')
  })
})
