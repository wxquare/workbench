import * as Vue from 'vue'
import { createRenderer, h, nextTick, ref, ssrContextKey } from 'vue'
import { compile } from '@vue/compiler-dom'
import { compileScript, parse } from '@vue/compiler-sfc'
import source from '../src/components/EvidenceReferences.vue?raw'
import { expect, it, vi } from 'vitest'
import EvidenceReferences from '../src/components/EvidenceReferences.vue'
import { ApiClient } from '../src/api'
interface Node { type: string; text: string; children: Node[]; parent: Node | null; props: Record<string, unknown> }
const node = (type: string, text = ''): Node => ({ type, text, children: [], parent: null, props: {} })
const renderer = createRenderer<Node, Node>({
  createElement: type => node(type), createText: text => node('#text', text), createComment: text => node('#comment', text),
  setText: (el, text) => { el.text = text }, setElementText: (el, text) => { el.text = text; el.children = [] },
  insert: (el, parent, anchor) => { el.parent = parent; const i = anchor ? parent.children.indexOf(anchor) : -1; if (i < 0) parent.children.push(el); else parent.children.splice(i, 0, el) },
  remove: el => { if (el.parent) el.parent.children.splice(el.parent.children.indexOf(el), 1) },
  parentNode: el => el.parent, nextSibling: el => el.parent?.children[el.parent.children.indexOf(el) + 1] ?? null,
  patchProp: (el, key, _old, value) => { el.props[key] = value },
})
const content = (el: Node): string => el.text + el.children.map(content).join('')
const all = (el: Node): Node[] => [el, ...el.children.flatMap(all)]
async function flush() { for (let i = 0; i < 8; i++) { await Promise.resolve(); await nextTick() } }
function mount(getEvidence: any, runId = ref('run-1'), token = ref('token')) {
  const root = node('root')
  const descriptor = parse(source).descriptor
  const bindings = compileScript(descriptor, { id: 'evidence-test' }).bindings
  const code = compile(descriptor.template!.content, { mode: 'function', prefixIdentifiers: true, bindingMetadata: bindings }).code
  const component = { ...EvidenceReferences, render: new Function('Vue', code)(Vue) }
  const app = renderer.createApp({ render: () => h(component, { runId: runId.value, token: token.value, refs: ['e1', 'e1', 'e2'], getEvidence }) })
  app.provide(ssrContextKey, { modules: new Set() })
  app.mount(root); return { app, root, runId, token }
}
const payload = { truncated: false, content: { data: { truncated: false, matches: [
  { source_id: 'a', title: 'Document A', source_uri: 'https://docs.example.test/a' },
  { source_id: 'b', title: '<script>unsafe</script>', source_uri: 'javascript:alert(1)' },
] } } }
it('loads unique evidence independently and presents sources without assigning claims', async () => {
  const getEvidence = vi.fn().mockImplementation((_run: string, id: string) => id === 'e1' ? Promise.resolve(payload) : Promise.reject(new Error('403')))
  const { app, root } = mount(getEvidence); await flush()
  expect(getEvidence).toHaveBeenCalledTimes(2)
  expect(content(root)).toContain('Document A'); expect(content(root)).toContain('来源 ID：a')
  expect(content(root)).toContain('来源 ID：b'); expect(content(root)).toContain('不可点击')
  expect(content(root)).toContain('不可用'); expect(content(root)).toContain('有引用不代表已证明语义正确')
  expect(all(root).filter(n => n.type === 'a').map(n => n.props.href)).toEqual(['https://docs.example.test/a'])
  app.unmount()
})
it('does not show truncated evidence or unsafe URL components', async () => {
  const getEvidence = vi.fn().mockResolvedValueOnce({ ...payload, truncated: true }).mockResolvedValueOnce({ truncated: false, content: { data: { matches: [{ source_id: 'c', title: 'C', source_uri: 'https://user:pass@example.test/a?secret=1' }] } } })
  const { app, root } = mount(getEvidence); await flush()
  expect(content(root)).toContain('截断'); expect(content(root)).not.toContain('Document A')
  expect(all(root).filter(n => n.type === 'a')).toHaveLength(0)
  app.unmount()
})
it('clears previous identity and ignores stale evidence responses', async () => {
  const resolves: ((value: unknown) => void)[] = []
  const getEvidence = vi.fn().mockImplementation((run: string) => run === 'run-1' ? new Promise(resolve => { resolves.push(resolve) }) : Promise.reject(new Error('403')))
  const { app, root, runId, token } = mount(getEvidence); await flush()
  runId.value = 'run-2'; token.value = 'new'; await flush()
  resolves.forEach(resolve => resolve(payload)); await flush()
  expect(content(root)).not.toContain('Document A'); expect(content(root)).toContain('不可用')
  app.unmount()
})
it('encodes run and evidence IDs in the authorized API request', async () => {
  const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => payload })
  const client = new ApiClient(() => 'token', fetcher)
  await client.evidence('run/1', 'e/1')
  expect(fetcher.mock.calls[0][0]).toBe('/v1/runs/run%2F1/evidence/e%2F1')
})

it('rechecks authorization and removes evidence after revocation without restoring stale responses', async () => {
  vi.useFakeTimers()
  let resolveStale!: (value: unknown) => void
  const getEvidence = vi.fn().mockImplementation((_run: string, id: string) => {
    if (id === 'e2') return Promise.reject(new Error('403'))
    const calls = getEvidence.mock.calls.filter((call: unknown[]) => call[1] === 'e1').length
    if (calls === 1) return Promise.resolve(payload)
    if (calls === 2) return new Promise(resolve => { resolveStale = resolve })
    return Promise.reject(new Error('403'))
  })
  const { app, root } = mount(getEvidence); await flush()
  expect(content(root)).toContain('Document A')
  await vi.advanceTimersByTimeAsync(30000); await flush()
  expect(content(root)).not.toContain('Document A')
  await vi.advanceTimersByTimeAsync(30000); await flush()
  expect(content(root)).toContain('不可用')
  resolveStale(payload); await flush()
  expect(content(root)).not.toContain('Document A')
  app.unmount()
  const count = getEvidence.mock.calls.length
  await vi.advanceTimersByTimeAsync(30000)
  expect(getEvidence).toHaveBeenCalledTimes(count)
  vi.useRealTimers()
})
