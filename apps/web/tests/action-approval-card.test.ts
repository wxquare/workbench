import { mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import ActionApprovalCard from '../src/components/ActionApprovalCard.vue'

it('renders target, arguments and complete hash before approval', async () => {
  const hash = 'a'.repeat(64)
  const wrapper = mount(ActionApprovalCard, {
    props: {
      detail: {
        arguments: { service: 'checkout' },
        target: { server: 'ticket', tool_name: 'CreateTicket' },
        payload_hash: hash,
      },
      disabled: false,
    },
  })
  expect(wrapper.text()).toContain('checkout')
  expect(wrapper.text()).toContain(hash)
  await wrapper.findAll('button')[0].trigger('click')
  expect(wrapper.emitted('decide')).toEqual([['approve', hash]])
})

it('does not emit while disabled', async () => {
  const wrapper = mount(ActionApprovalCard, {
    props: {
      detail: {
        arguments: {},
        target: {},
        payload_hash: 'b'.repeat(64),
      },
      disabled: true,
    },
  })
  await wrapper.find('button').trigger('click')
  expect(wrapper.emitted('decide')).toBeUndefined()
})
