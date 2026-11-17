import { describe, expect, it, vi } from 'vitest'
import { SkillApi } from '../src/skills-api'

describe('shared skills API', () => {
  it('saves a revision and reuses the idempotency key on retry', async () => {
    const request = vi.fn().mockRejectedValueOnce(new Error('network')).mockResolvedValueOnce({ revision: 2 })
    const api = new SkillApi({ request }, () => 'key-one')
    await expect(api.save('skill', 1, { description: 'd', content: 'text' })).rejects.toThrow('network')
    await api.save('skill', 1, { description: 'd', content: 'text' })
    expect(request.mock.calls[1][0]).toBe('/v1/skills/skill')
    expect(request.mock.calls[1][1].headers).toMatchObject({ 'If-Match': '1', 'Idempotency-Key': 'key-one' })
  })
})
