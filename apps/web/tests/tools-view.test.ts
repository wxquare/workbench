import {expect, it} from 'vitest'
import source from '../src/views/Tools.vue?raw'

it('does not expose deployment endpoints or secrets as editable fields', () => {
  expect(source).not.toContain('endpoint</')
  expect(source).not.toContain('secret_ref')
  expect(source).toContain('完整 Schema')
})
