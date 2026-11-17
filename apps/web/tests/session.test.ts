import { expect, it, vi } from 'vitest'

it('migrates the existing bearer token to the workbench session key', async () => {
  const entries = new Map([['dod-bearer-token', 'existing-token']])
  vi.stubGlobal('sessionStorage', {
    getItem: (key: string) => entries.get(key) ?? null,
    setItem: (key: string, value: string) => entries.set(key, value),
    removeItem: (key: string) => entries.delete(key),
  })
  vi.resetModules()

  const { bearerToken } = await import('../src/session')

  expect(bearerToken.value).toBe('existing-token')
  expect(entries.get('workbench-bearer-token')).toBe('existing-token')
  expect(entries.has('dod-bearer-token')).toBe(false)
  vi.unstubAllGlobals()
})
