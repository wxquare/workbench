import { expect, it, vi } from 'vitest'
import router from '../src/router'
import Agents from '../src/views/Agents.vue'

vi.mock('../src/session', async () => {
  const { ref } = await import('vue')
  return { bearerToken: ref(''), api: {} }
})

vi.mock('vue-router', async importOriginal => {
  const actual = await importOriginal<typeof import('vue-router')>()
  return { ...actual, createWebHistory: actual.createMemoryHistory }
})

it('exposes agent management at its canonical app route', async () => {
  await router.push({ name: 'agents' })
  expect(router.resolve({ name: 'agents' }).href).toBe('/app/agents')
  expect(router.currentRoute.value.matched[0].components?.default).toBe(Agents)
  expect(router.getRoutes().filter(route => route.components?.default === Agents).map(route => route.path))
    .toEqual(['/agents'])
})

it('preserves a knowledge citation exact version and fragment in its route', () => {
  const route = router.resolve({ name: 'knowledge-version', params: { id: 'entry', version: '2' }, hash: '#chunk' })
  expect(route.href).toBe('/app/knowledge/entry/versions/2#chunk')
})

it('provides graph catalog and detail routes', () => {
  expect(router.resolve({ name: 'graphs' }).href).toBe('/app/graphs')
  expect(router.resolve({ name: 'graph-detail', params: { id: 'sop.conditional' } }).href).toBe('/app/graphs/sop.conditional')
})
