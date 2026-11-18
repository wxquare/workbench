<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api, bearerToken } from '../session'
import { agentName, type Agent } from '../api'
import { openRunStream } from '../sse'
import { useRunDetail } from '../composables/useRunDetail'
import StreamingAnswer from '../components/StreamingAnswer.vue'
import RunActions from '../components/RunActions.vue'
import WaitingInputForm from '../components/WaitingInputForm.vue'
import ApprovalCard from '../components/ApprovalCard.vue'
import { buildInputResume, buildApprovalResume } from '../run-detail-state'
import { createRunActions } from '../run-actions'
import { listMessages } from '../threads-api'

const route = useRoute()
const runId = computed(() => String(route.params.id))
const { run, error, loading, connection, draft, waiting, refresh, applyRunView } = useRunDetail(runId, bearerToken, { getRun: id => api.run(id), stream: openRunStream })
const actions = createRunActions({ cancel: (id, key) => api.cancelRun(id, key), resume: (id, body, key) => api.resumeRun(id, body, key), refresh, apply: applyRunView, current: () => run.value })
const { busy, pending, error: actionError, readOnly } = actions
const question = ref(''), now = ref(Date.now())
const timer = setInterval(() => { now.value = Date.now() }, 1000)
onUnmounted(() => { clearInterval(timer); questionGeneration++; actions.reset() })
const remaining = computed(() => waiting.value && waiting.value.kind !== 'unsupported' ? Math.max(0, waiting.value.expiresAt - now.value) : null)
const disabled = computed(() => busy.value || pending.value || readOnly.value || remaining.value === 0)
watch(() => [runId.value, bearerToken.value, run.value?.waiting?.interrupt_id, run.value?.waiting?.revision, run.value?.revision], () => actions.reset())
watch(remaining, (value, old) => { if (value === 0 && old !== 0) void refresh() })
let questionGeneration = 0
watch(() => [run.value?.thread_id, run.value?.run_id, bearerToken.value], async () => {
  const current = ++questionGeneration, selected = run.value
  question.value = ''
  if (!selected || !bearerToken.value) return
  try {
    const seen = new Set<string>(); let after: string | undefined
    do {
      const page = await listMessages(api, selected.thread_id, after)
      if (current !== questionGeneration) return
      const found = page.items.find(item => item.run_id === selected.run_id && item.role === 'user' && item.input?.text)
      if (found) { question.value = found.input!.text; return }
      after = page.next_cursor ?? undefined
      if (after && seen.has(after)) return
      if (after) seen.add(after)
    } while (after)
  } catch { if (current === questionGeneration) question.value = '' }
}, { immediate: true })
function submitInput(region: string) { if (waiting.value?.kind === 'input') void actions.resume(buildInputResume(waiting.value, region)) }
function decide(decision: 'approve' | 'reject') { if (waiting.value?.kind === 'approval') void actions.resume(buildApprovalResume(waiting.value, decision)) }
const agents = ref<Agent[]>([])
let agentGeneration = 0
watch(bearerToken, async () => {
  const current = ++agentGeneration
  if (!bearerToken.value) { agents.value = []; return }
  try { const value = await api.agents(); if (current === agentGeneration) agents.value = value }
  catch { if (current === agentGeneration) agents.value = [] }
}, { immediate: true })
const labels: Record<string, string> = { queued: '排队中', running: '执行中', finalizing: '整理结果中', succeeded: '已完成', failed: '失败', cancelled: '已取消', expired: '已过期', timed_out: '已超时', waiting_input: '等待输入', waiting_approval: '等待审批', cancelling: '取消中', partial: '部分完成' }
</script>
<template>
  <router-link to="/" class="back-link">← 返回问答</router-link>
  <div class="page-heading detail-heading"><div><div class="eyebrow">RUN DETAIL <span></span> 任务详情</div><h1>问答任务</h1><p>{{ route.params.id }}</p></div><button type="button" :disabled="loading || !bearerToken" @click="refresh">刷新</button></div>
  <p v-if="error" class="form-error" role="alert">{{ error }}</p>
  <p v-if="loading && !run" class="form-note">正在加载任务…</p>
  <template v-if="run">
    <RunActions :status="run.status" :busy="busy || pending" @cancel="actions.cancel" />
    <p v-if="actionError" class="form-error" role="alert">{{ actionError }}</p>
    <p v-if="remaining !== null" class="form-note">剩余有效时间：{{ Math.ceil(remaining / 1000) }} 秒</p>
    <WaitingInputForm v-if="waiting?.kind === 'input'" :waiting="waiting" :disabled="disabled" @submit="submitInput" />
    <ApprovalCard v-else-if="waiting?.kind === 'approval'" :waiting="waiting" :disabled="disabled" :question="question" @decide="decide" />
    <p v-else-if="waiting?.kind === 'unsupported'" class="detail-warning">{{ waiting.reason }}，请通过 API 处理。</p>
    <div class="detail-grid"><div class="detail-primary"><section class="panel detail-panel"><div class="panel-heading"><div><h2>结果</h2><p>已保存的任务结果</p></div></div>
      <p v-if="connection === 'polling'" class="form-note">实时连接不可用，正在轮询更新。</p>
      <StreamingAnswer v-if="draft && !(run.outcome && connection === 'closed')" :text="draft" />
      <template v-if="run.outcome"><div class="summary-box"><p class="result-text">{{ run.outcome.summary }}</p></div>
        <div v-if="run.outcome.findings?.length" class="result-section"><h3>发现</h3><ul><li v-for="(item, index) in run.outcome.findings" :key="index">{{ item.summary }}</li></ul></div>
        <div v-if="run.outcome.unresolved_items?.length" class="result-section"><h3>未完成事项</h3><ul><li v-for="item in run.outcome.unresolved_items" :key="item">{{ item }}</li></ul></div>
        <div v-if="run.outcome.knowledge_gaps?.length" class="result-section"><h3>知识缺口</h3><ul><li v-for="item in run.outcome.knowledge_gaps" :key="item">{{ item }}</li></ul></div>
        <div v-if="run.outcome.evidence_refs?.length" class="result-section"><h3>证据 ID</h3><ul><li v-for="item in run.outcome.evidence_refs" :key="item">{{ item }}</li></ul></div>
      </template><p v-else class="form-note">尚无已保存的结果。</p>
    </section></div><aside class="detail-secondary"><section class="panel info-panel"><h2>任务信息</h2><dl><div><dt>状态</dt><dd>{{ labels[run.status] ?? run.status }}</dd></div><div><dt>阶段</dt><dd>{{ run.phase ?? '—' }}</dd></div><div class="agent-info"><dt>Agent</dt><dd>{{ agentName(run.agent_id, agents) }}<small v-if="agentName(run.agent_id, agents) !== run.agent_id">{{ run.agent_id }}</small></dd></div><div><dt>创建时间</dt><dd>{{ new Date(run.created_at).toLocaleString() }}</dd></div></dl></section></aside></div>
  </template>
</template>

<style scoped>
.agent-info dd { text-align: right; min-width: 0; overflow-wrap: anywhere; }
.agent-info small { display: block; margin-top: 4px; color: #84919c; }
</style>
