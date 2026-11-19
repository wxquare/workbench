import { createSSRApp, ref } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createMemoryHistory, createRouter } from 'vue-router'
import { expect, it, vi } from 'vitest'
import App from '../src/App.vue'

vi.mock('../src/session', () => ({ bearerToken: ref(''), setBearerToken: vi.fn() }))

it('shows the workbench brand and links to agent management under the app base', async () => {
  const router = createRouter({ history: createMemoryHistory('/app/'), routes: [{ path: '/:pathMatch(.*)*', component: { template: '<div />' } }] })
  const app = createSSRApp(App).use(router)
  app.component('ElIcon', { template: '<span><slot /></span>' })
  const html = await renderToString(app)
  expect(html).toContain('我的工作台')
  expect(html).toContain('Agent 管理')
  expect(html).toContain('href="/app/agents"')
  expect(html).toContain('工作流')
  expect(html).toContain('收起侧栏')
  expect(html).not.toContain('DoD Agent')
})
