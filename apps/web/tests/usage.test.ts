import {expect, it} from 'vitest'
import {formatUsageCost} from '../src/usage'

it('keeps unknown cost distinct from confirmed zero', () => {
  expect(formatUsageCost({amount_micros: null, currency: null, cost_pending: true, known_costs: {}}))
    .toBe('计费待确认')
  expect(formatUsageCost({amount_micros: 0, currency: 'USD', cost_pending: false, known_costs: {USD: 0}}))
    .toBe('USD 0.000000')
})

it('does not add mixed currencies', () => {
  expect(formatUsageCost({known_costs: {USD: 1, CNY: 2}, cost_pending: true}))
    .toBe('USD 0.000001 + CNY 0.000002')
})
