import {expect, it, vi} from 'vitest'
import {ToolApi} from '../src/tools-api'

it('reuses a discovery key after an uncertain response', async () => {
  const request = vi.fn().mockRejectedValueOnce(new Error('network')).mockResolvedValueOnce({job_id: 'job'})
  const service = new ToolApi({request} as never, () => 'stable')
  await expect(service.discover('agent', 'local', 'reader')).rejects.toThrow()
  await service.discover('agent', 'local', 'reader')
  expect(request.mock.calls[0][1].headers['Idempotency-Key']).toBe('stable')
  expect(request.mock.calls[1][1].headers['Idempotency-Key']).toBe('stable')
})
