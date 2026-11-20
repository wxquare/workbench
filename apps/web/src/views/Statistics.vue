<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { api, bearerToken } from '../session'
import type { Agent } from '../api'
import { getStatistics, type StatisticsSummary } from '../statistics-api'
import { formatUsageCost } from '../usage'

const start = ref(''), end = ref(''), agent = ref('')
const agents = ref<Agent[]>([])
const report = ref<StatisticsSummary | null>(null)
const loading = ref(false), error = ref('')
let generation = 0

function dateInput(date: Date) {
  return date.toISOString().slice(0, 10)
}
const now = new Date()
const sevenDaysAgo = new Date(now.getTime() - 6 * 86400000)
start.value = dateInput(sevenDaysAgo)
end.value = dateInput(now)

const rate = (value: number | null) => value === null ? '—' : `${(value * 100).toFixed(1)}%`
const latency = computed(() => report.value?.latency.average_ms === null
  ? '—'
  : `${Math.round((report.value?.latency.average_ms ?? 0) / 1000)} 秒`)
const cost = computed(() => {
  const value = report.value?.costs
  if (!value) return '—'
  const known = Object.entries(value.known)
  if (!known.length) return value.pending_run_count ? '计费待确认' : '—'
  const formatted = known.map(([currency, amount]) =>
    formatUsageCost({known_costs: {[currency]: amount}, cost_pending: false}),
  ).join(' + ')
  return value.pending_run_count ? `${formatted}；${value.pending_run_count} 个 Run 待确认` : formatted
})
const chart = computed(() => {
  const days = report.value?.trend ?? []
  const maximum = Math.max(1, ...days.flatMap(item => [item.succeeded, item.partial, item.runs]))
  return days.map((item, index) => ({
    ...item,
    x: days.length > 1 ? index * (620 / (days.length - 1)) : 310,
    taskY: 120 - item.runs / maximum * 90,
    successY: 120 - item.succeeded / maximum * 90,
  }))
})

async function load() {
  const current = ++generation
  report.value = null
  error.value = ''
  if (!bearerToken.value || !start.value || !end.value) return
  const startAt = new Date(`${start.value}T00:00:00+08:00`).toISOString()
  const endAt = new Date(`${end.value}T00:00:00+08:00`)
  endAt.setDate(endAt.getDate() + 1)
  if (endAt.getTime() - new Date(startAt).getTime() > 90 * 86400000) {
    error.value = '统计范围不能超过 90 天。'
    return
  }
  loading.value = true
  try {
    const value = await getStatistics(api, startAt, endAt.toISOString(), agent.value || undefined)
    if (current === generation) report.value = value
  } catch (e) {
    if (current === generation) error.value = e instanceof Error ? e.message : '统计加载失败'
  } finally {
    if (current === generation) loading.value = false
  }
}

async function loadAgents() {
  if (!bearerToken.value) { agents.value = []; return }
  try { agents.value = await api.agents() } catch { agents.value = [] }
}
watch(bearerToken, () => { agents.value = []; agent.value = ''; void loadAgents(); void load() }, {immediate: true})
watch([start, end, agent], () => void load())
onUnmounted(() => generation++)
</script>

<template>
  <div class="page-heading"><div><div class="eyebrow">OPERATIONS <span></span> 统计分析</div><h1>运行与知识质量</h1><p>按当前可见 Agent 和 Run 创建时间统计。</p></div><button type="button" :disabled="loading" @click="load">刷新</button></div>
  <section class="panel stats-filters">
    <label>开始日期<input v-model="start" type="date" /></label>
    <label>结束日期<input v-model="end" type="date" /></label>
    <label>Agent<select v-model="agent"><option value="">全部可见 Agent</option><option v-for="item in agents" :key="item.agent_id" :value="item.agent_id">{{ item.name }}</option></select></label>
  </section>
  <p v-if="error" class="form-error" role="alert">{{ error }}</p>
  <p v-if="loading" class="form-note">正在计算统计…</p>
  <template v-if="report">
    <div class="stats-grid">
      <article class="panel stat-card"><span>Run 总数</span><strong>{{ report.counts.runs }}</strong><small>执行中 {{ report.counts.active }}</small></article>
      <article class="panel stat-card"><span>成功率</span><strong>{{ rate(report.rates.success) }}</strong><small>分母为终态 {{ report.counts.terminal }}</small></article>
      <article class="panel stat-card"><span>部分完成率</span><strong>{{ rate(report.rates.partial) }}</strong><small>与成功分开统计</small></article>
      <article class="panel stat-card"><span>平均耗时</span><strong>{{ latency }}</strong><small>仅终态已启动 Run</small></article>
      <article class="panel stat-card"><span>已确认费用</span><strong class="cost">{{ cost }}</strong><small>多币种不汇总</small></article>
      <article class="panel stat-card"><span>负反馈率</span><strong>{{ rate(report.feedback.negative_rate) }}</strong><small>{{ report.feedback.subjects }} 个 Run/用户组合有反馈</small></article>
    </div>
    <div class="stats-detail">
      <section class="panel"><h2>每日趋势</h2><svg v-if="report.trend.length" viewBox="0 0 640 150" role="img" aria-label="每日 Run 与成功数量"><polyline :points="chart.map(item => `${item.x},${item.taskY}`).join(' ')" fill="none" stroke="#506f89" stroke-width="3" /><polyline :points="chart.map(item => `${item.x},${item.successY}`).join(' ')" fill="none" stroke="#2d8b6b" stroke-width="3" /><g v-for="item in chart" :key="item.day"><circle :cx="item.x" :cy="item.taskY" r="3" fill="#506f89" /><circle :cx="item.x" :cy="item.successY" r="3" fill="#2d8b6b" /><text :x="item.x" y="143" text-anchor="middle">{{ item.day.slice(5) }}</text></g></svg><p v-else>暂无趋势数据。</p><p class="legend"><i class="runs"></i>Run 总数 <i class="success"></i>成功</p></section>
      <section class="panel"><h2>知识缺口</h2><dl><div><dt>待处理</dt><dd>{{ report.gaps.open }}</dd></div><div><dt>已解决</dt><dd>{{ report.gaps.resolved }}</dd></div><div><dt>已忽略</dt><dd>{{ report.gaps.dismissed }}</dd></div></dl><h2>失败原因</h2><ul><li v-for="[code, count] in report.failure_top5" :key="code">{{ code }} <strong>{{ count }}</strong></li><li v-if="!report.failure_top5.length">暂无失败。</li></ul></section>
    </div>
    <p class="stats-footnote">分母、时间范围与权限口径为 stats-v1；统计生成于 {{ new Date(report.generated_at).toLocaleString() }}。</p>
  </template>
</template>

<style scoped>
.stats-filters { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; padding: 18px; margin-bottom: 18px; }
.stats-filters label { display: grid; gap: 7px; color: #596975; }
.stats-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }
.stat-card { padding: 18px; display: grid; gap: 7px; }
.stat-card span, .stat-card small { color: #687985; }
.stat-card strong { font-size: 28px; color: #21323d; }
.stat-card .cost { font-size: 20px; overflow-wrap: anywhere; }
.stats-detail { display: grid; grid-template-columns: 2fr 1fr; gap: 16px; margin-top: 16px; }
.stats-detail .panel { padding: 18px; }
.stats-detail svg { width: 100%; height: auto; }
.stats-detail text { font-size: 10px; fill: #71818d; }
.legend { display: flex; gap: 16px; align-items: center; }
.legend i { width: 12px; height: 3px; display: inline-block; margin-right: 5px; }
.legend .runs { background: #506f89; }.legend .success { background: #2d8b6b; }
.stats-footnote { color: #71818d; font-size: 13px; }
@media (max-width: 800px) { .stats-filters, .stats-grid, .stats-detail { grid-template-columns: 1fr; } }
</style>
