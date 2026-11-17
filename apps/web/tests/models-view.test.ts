import { expect, it, vi } from 'vitest'
import { createSSRApp, reactive } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { copyModelDraft, type ModelDraft } from '../src/models-api'

it('copies a reactive model draft without sharing nested state', () => {
  const draft = reactive({ name: 'test', provider_options: { thinking: { type: 'disabled' } } }) as ModelDraft
  const copy = copyModelDraft(draft)
  copy.name = 'changed'
  expect(draft.name).toBe('test')
  expect(copy.provider_options).not.toBe(draft.provider_options)
})

it('renders the model administration agent selector', async () => {
  vi.stubGlobal('sessionStorage', { getItem: () => null, removeItem: vi.fn() })
  try {
    const { default: Models } = await import('../src/views/Models.vue')
    const html = await renderToString(createSSRApp(Models))
    expect(html).toContain('Agent')
    expect(html).toContain('模型配置')
  } finally { vi.unstubAllGlobals() }
})
