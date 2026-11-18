<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'

const props = defineProps<{ runId: string; token: string; refs: string[]; getEvidence: (runId: string, evidenceId: string) => Promise<unknown> }>()
type Source = { id: string; title: string; url: string | null }
type Entry = { id: string; status: string; sources: Source[] }
const entries = ref<Entry[]>([])
let generation = 0
let timer: ReturnType<typeof setInterval> | undefined
function object(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
}
function safeUrl(value: unknown): string | null {
  if (typeof value !== 'string') return null
  try {
    const url = new URL(value)
    return url.protocol === 'https:' && !url.username && !url.password && !url.search && !url.hash ? url.href : null
  } catch { return null }
}
function parse(id: string, value: unknown): Entry {
  const payload = object(value), content = object(payload?.content), data = object(content?.data)
  if (payload?.truncated === true || data?.truncated === true) return { id, status: '证据已截断，不可用', sources: [] }
  if (!payload || !data || !Array.isArray(data.matches)) return { id, status: '证据不可用', sources: [] }
  const sources = data.matches.flatMap((item): Source[] => {
    const match = object(item)
    if (!match || typeof match.source_id !== 'string' || !match.source_id.trim() || typeof match.title !== 'string') return []
    return [{ id: match.source_id, title: match.title || '未命名来源', url: safeUrl(match.source_uri) }]
  })
  return { id, status: sources.length ? '' : '无可用来源', sources }
}
watch(() => [props.runId, props.token, JSON.stringify(props.refs)] as const, ([runId, token]) => {
  generation++
  clearInterval(timer)
  entries.value = []
  if (!runId || !token.trim()) return
  const ids = [...new Set(props.refs.filter(id => typeof id === 'string' && id))]
  function refresh() {
    const current = ++generation
    entries.value = ids.map(id => ({ id, status: '正在加载证据…', sources: [] }))
    ids.forEach(async (id, index) => {
      let entry: Entry
      try { entry = parse(id, await props.getEvidence(runId, id)) }
      catch { entry = { id, status: '证据不可用（可能无权限或已移除）', sources: [] } }
      if (current === generation) entries.value[index] = entry
    })
  }
  refresh()
  if (ids.length) timer = setInterval(refresh, 30000)
}, { immediate: true, flush: 'sync' })
onUnmounted(() => { generation++; clearInterval(timer) })
</script>
<template>
  <section v-if="entries.length" class="result-section evidence-references">
    <h3>证据来源</h3>
    <p>有引用不代表已证明语义正确。以下来源属于所列证据，不能仅凭此列表确定其支持哪条结论。</p>
    <div v-for="entry in entries" :key="entry.id" class="evidence-entry">
      <p>证据 ID：{{ entry.id }}</p>
      <p v-if="entry.status">{{ entry.status }}</p>
      <ul v-else><li v-for="(source, index) in entry.sources" :key="index">
        <span>来源 ID：{{ source.id }} — </span><a v-if="source.url" :href="source.url" target="_blank" rel="noopener noreferrer">{{ source.title }}</a><span v-else>{{ source.title }}（链接不可点击）</span>
      </li></ul>
    </div>
  </section>
</template>
<style scoped>
.evidence-references, .evidence-entry { overflow-wrap: anywhere; }
</style>
