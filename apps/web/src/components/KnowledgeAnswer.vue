<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api, bearerToken } from '../session'
import type { KnowledgeAnswer } from '../api'
import { knowledgeRoute, verifiedCitations, type EvidenceContent, type VisibleCitation } from '../knowledge-citations'

const props = defineProps<{ runId: string; answer: KnowledgeAnswer }>()
const evidence = ref<Record<string, EvidenceContent>>({}), loading = ref(false), unavailable = ref(false)
let generation = 0
watch(() => [props.runId, props.answer, bearerToken.value], async () => {
  const current = ++generation; evidence.value = {}; unavailable.value = false; loading.value = false
  if (!bearerToken.value || !props.answer) return
  const refs = [...new Set(props.answer.claims.flatMap(claim => claim.citations.map(citation => citation.evidence_ref)))]
  loading.value = true
  try {
    const values = await Promise.all(refs.map(ref => api.request<EvidenceContent>(`/v1/runs/${encodeURIComponent(props.runId)}/evidence/${encodeURIComponent(ref)}`)))
    if (current === generation) evidence.value = Object.fromEntries(values.map(value => [value.evidence_id, value]))
  } catch { if (current === generation) { evidence.value = {}; unavailable.value = true } }
  finally { if (current === generation) loading.value = false }
}, { immediate: true })
const claims = computed(() => {
  if (loading.value || unavailable.value || !bearerToken.value) return null
  const result: { text: string; citations: VisibleCitation[] }[] = []
  for (const claim of props.answer.claims) {
    const citations = verifiedCitations(claim.citations, evidence.value)
    if (!citations) return null
    result.push({ text: claim.text, citations })
  }
  return result
})
</script>

<template>
  <section class="knowledge-answer"><p v-if="loading" class="form-note">正在核对引用权限…</p><p v-else-if="!claims" class="form-note">引用不可用或权限已变化，当前无法显示该回答。</p><template v-else><p class="answer-review">引用已核验 · 模型复核</p><article v-for="(claim, index) in claims" :key="index"><p>{{ claim.text }}</p><blockquote v-for="citation in claim.citations" :key="citation.evidence_ref + citation.source_id + citation.quote"><p>{{ citation.quote }}</p><small>{{ citation.title }}<template v-if="citation.source_locator"> · {{ citation.source_locator }}</template></small><router-link v-if="knowledgeRoute(citation.source_id)" :to="{ name: 'knowledge-version', params: { id: knowledgeRoute(citation.source_id)?.id, version: knowledgeRoute(citation.source_id)?.version }, hash: knowledgeRoute(citation.source_id)?.hash }">查看原文 ↗</router-link></blockquote></article></template></section>
</template>

<style scoped>
.knowledge-answer{padding:0 20px 18px}.answer-review{font-size:11px;color:#53886e}.knowledge-answer article{margin-top:12px;font-size:13px}.knowledge-answer blockquote{margin:10px 0;padding:10px 12px;background:#f4f7ff;border-left:3px solid #7790df;white-space:pre-wrap;overflow-wrap:anywhere}.knowledge-answer blockquote p{margin:0 0 8px}.knowledge-answer small{color:#8494a8}.knowledge-answer a{color:#5675d7;margin-left:10px}
</style>
