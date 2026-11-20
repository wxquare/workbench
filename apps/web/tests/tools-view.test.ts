import {expect, it} from 'vitest'
import source from '../src/views/Tools.vue?raw'

it('does not expose deployment endpoints or secrets as editable fields', () => {
  expect(source).not.toContain('endpoint</')
  expect(source).not.toContain('secret_ref')
  expect(source).toContain('完整 Schema')
})

it('describes deployment mapping policy without exposing deployment addresses', () => {
  expect(source).toContain('mapping_policies')
  expect(source).toContain('模型仅可填写声明字段')
  expect(source).not.toContain('secret_ref')
  expect(source).not.toContain('endpoint')
})
