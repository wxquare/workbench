<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api, bearerToken } from '../session'
import { agentName, ApiError, isRunActive, type RunView, type Agent } from '../api'

const route = useRoute()
const run = ref<RunView | null>(null), error = ref(''), loading = ref(false)
const agents = ref<Agent[]>([])
let timer: ReturnType<typeof setTimeout> | undefined
let generation = 0
const labels: Record<string, string> = { queued: '排队中', running: '执行中', finalizing: '整理结果中', succeeded: '已完成', failed: '失败', cancelled: '已取消', expired: '已过期', timed_out: '已超时', waiting_input: '等待输入', waiting_approval: '等待审批', cancelling: '取消中', partial: '部分完成' }
async function refresh(current = generation) {
  if (!bearerToken.value) { loading.value = false; error.value = '请先提供 Bearer Token。'; return }
  loading.value = true
  try {
    const result = await api.run(String(route.params.id))
    if (current !== generation) return
    run.value = result; error.value = ''
    if (isRunActive(result.status)) timer = setTimeout(() => void refresh(current), 3000)
  } catch (e) {
    if (current !== generation) return
    error.value = e instanceof Error ? e.message : '加载任务失败'
    if (e instanceof ApiError && (e.status === 401 || e.status === 403 || e.status === 404)) run.value = null
    if (!(e instanceof ApiError) || e.status >= 500) timer = setTimeout(() => void refresh(current), 5000)
  } finally { if (current === generation) loading.value = false }
}
async function loadAgents(current: number) {
  try {
    const result = await api.agents()
    if (current === generation) agents.value = result
  } catch { if (current === generation) agents.value = [] }
}
function restart() { generation++; clearTimeout(timer); run.value = null; agents.value = []; error.value = ''; if (bearerToken.value) void loadAgents(generation); void refresh(generation) }
function manualRefresh() { generation++; clearTimeout(timer); void loadAgents(generation); void refresh(generation) }
watch(() => [route.params.id, bearerToken.value], restart, { immediate: true })
onUnmounted(() => { generation++; clearTimeout(timer) })
</script>
<template>
  <router-link to="/" class="back-link">← 返回问答</router-link>
  <div class="page-heading detail-heading"><div><div class="eyebrow">RUN DETAIL <span></span> 任务详情</div><h1>问答任务</h1><p>{{ route.params.id }}</p></div><button type="button" :disabled="loading || !bearerToken" @click="manualRefresh">刷新</button></div>
  <p v-if="error" class="form-error" role="alert">{{ error }}</p>
  <p v-if="loading && !run" class="form-note">正在加载任务…</p>
  <template v-if="run">
    <div class="detail-grid"><div class="detail-primary"><section class="panel detail-panel"><div class="panel-heading"><div><h2>结果</h2><p>已保存的任务结果</p></div></div>
      <template v-if="run.outcome"><div class="summary-box"><p class="result-text">{{ run.outcome.summary }}</p></div>
        <div v-if="run.outcome.findings?.length" class="result-section"><h3>发现</h3><ul><li v-for="(item, index) in run.outcome.findings" :key="index">{{ item.summary }}</li></ul></div>
        <div v-if="run.outcome.unresolved_items?.length" class="result-section"><h3>未完成事项</h3><ul><li v-for="item in run.outcome.unresolved_items" :key="item">{{ item }}</li></ul></div>
        <div v-if="run.outcome.knowledge_gaps?.length" class="result-section"><h3>知识缺口</h3><ul><li v-for="item in run.outcome.knowledge_gaps" :key="item">{{ item }}</li></ul></div>
        <div v-if="run.outcome.evidence_refs?.length" class="result-section"><h3>证据 ID</h3><ul><li v-for="item in run.outcome.evidence_refs" :key="item">{{ item }}</li></ul></div>
      </template><p v-else class="form-note">尚无已保存的结果。</p>
      <p v-if="run.waiting" class="detail-warning">任务正在等待输入或审批，当前页面暂不支持继续操作。</p>
    </section></div><aside class="detail-secondary"><section class="panel info-panel"><h2>任务信息</h2><dl><div><dt>状态</dt><dd>{{ labels[run.status] ?? run.status }}</dd></div><div><dt>阶段</dt><dd>{{ run.phase ?? '—' }}</dd></div><div class="agent-info"><dt>Agent</dt><dd>{{ agentName(run.agent_id, agents) }}<small v-if="agentName(run.agent_id, agents) !== run.agent_id">{{ run.agent_id }}</small></dd></div><div><dt>创建时间</dt><dd>{{ new Date(run.created_at).toLocaleString() }}</dd></div></dl></section></aside></div>
  </template>
</template>

<style scoped>
.agent-info dd { text-align: right; min-width: 0; overflow-wrap: anywhere; }
.agent-info small { display: block; margin-top: 4px; color: #84919c; }
</style>
