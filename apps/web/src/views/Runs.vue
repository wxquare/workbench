<script setup lang="ts">
import { ref, watch } from 'vue'
import { Search } from '@element-plus/icons-vue'
import { api, bearerToken } from '../session'
import { agentName, type Agent } from '../api'
import { listRuns, type RunItem } from '../runs-api'
import { formatUsageCost } from '../usage'

const agents = ref<Agent[]>([]), agent = ref('')
const query = ref(''), status = ref(''), runs = ref<RunItem[]>([])
const cursor = ref<string | null>(null), loading = ref(false), error = ref('')
let generation = 0, agentGeneration = 0
let activeQuery = ''
const labels: Record<string, string> = { queued: '排队中', running: '执行中', finalizing: '正在完成', cancelling: '取消中', waiting_input: '等待输入', waiting_approval: '等待审批', succeeded: '已完成', partial: '部分完成', failed: '失败', cancelled: '已取消', expired: '已过期', timed_out: '已超时' }

async function load(append = false) {
  const current = ++generation
  if (!agent.value || !bearerToken.value) { runs.value = []; cursor.value = null; loading.value = false; return }
  loading.value = true
  error.value = ''
  try {
    const nextQuery = append ? activeQuery : query.value.trim()
    const page = await listRuns(api, agent.value, { query: nextQuery, status: status.value, after: append ? cursor.value ?? undefined : undefined })
    if (current !== generation) return
    activeQuery = nextQuery
    runs.value = append ? [...runs.value, ...page.items] : page.items
    cursor.value = page.next_cursor
  } catch (e) {
    if (current !== generation) return
    error.value = e instanceof Error ? e.message : '加载任务失败。'
    if (!append) { runs.value = []; cursor.value = null }
  } finally { if (current === generation) loading.value = false }
}

async function loadAgents() {
  const current = ++agentGeneration
  generation++
  runs.value = []; cursor.value = null; error.value = ''; agent.value = ''
  if (!bearerToken.value) { agents.value = []; return }
  loading.value = true
  try {
    const items = await api.agents()
    if (current !== agentGeneration) return
    agents.value = items
    agent.value = items[0]?.agent_id ?? ''
    if (!agent.value) loading.value = false
  } catch (e) {
    if (current !== agentGeneration) return
    agents.value = []; loading.value = false
    error.value = e instanceof Error ? e.message : '加载 Agent 失败。'
  }
}
watch(bearerToken, () => void loadAgents(), { immediate: true })
watch([agent, status], () => void load())
</script>

<template>
  <div class="page-heading"><div><div class="eyebrow">AGENT <span></span> 任务记录</div><h1>任务记录</h1></div><router-link to="/" class="text-link">发起问答 →</router-link></div>
  <section class="panel list-panel">
    <div class="panel-heading"><div><h2>任务</h2></div><span class="updated">{{ runs.length }} 条已加载</span></div>
    <p v-if="error" class="form-error" role="alert">{{ error }}</p>
    <p v-if="!bearerToken" class="form-note">请先提供 Bearer Token。</p>
    <div class="filters">
      <el-select v-model="agent" aria-label="选择 Agent" placeholder="选择 Agent" :disabled="!bearerToken"><el-option v-for="item in agents" :key="item.agent_id" :label="item.name" :value="item.agent_id" /></el-select>
      <el-select v-model="status" aria-label="筛选状态" placeholder="全部状态"><el-option label="全部状态" value="" /><el-option v-for="(label, value) in labels" :key="value" :label="label" :value="value" /></el-select>
      <el-input v-model="query" :prefix-icon="Search" placeholder="搜索问题或任务 ID" clearable aria-label="搜索任务" @keyup.enter="load()" />
      <button type="button" :disabled="loading || !agent" @click="load()">搜索</button>
    </div>
    <div class="table-wrap"><table><thead><tr><th>任务</th><th>Agent</th><th>类型</th><th>状态</th><th>费用</th><th>创建时间</th></tr></thead><tbody>
      <tr v-for="run in runs" :key="run.run_id"><td><router-link :to="`/runs/${run.run_id}`" class="run-name">{{ run.title || '未命名任务' }}</router-link><div class="run-id">{{ run.run_id }}</div></td><td>{{ agentName(run.agent_id, agents) }}</td><td>{{ run.mode ?? '—' }}</td><td>{{ labels[run.status] ?? run.status }}</td><td>{{ run.usage_summary ? formatUsageCost(run.usage_summary) : '—' }}</td><td>{{ new Date(run.created_at).toLocaleString() }}</td></tr>
    </tbody></table><div v-if="!loading && !runs.length && agent && !error" class="empty">暂无任务</div></div>
    <p v-if="loading" class="form-note">正在加载任务…</p>
    <button v-if="cursor" type="button" :disabled="loading" @click="load(true)">加载更多</button>
  </section>
</template>
