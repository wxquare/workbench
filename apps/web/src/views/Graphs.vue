<script setup lang="ts">
import { ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { api, bearerToken } from '../session'
import { agentError } from '../agents-api'
import { GraphApi, type GraphSummary, type GraphDetail } from '../graphs-api'
import GraphView from '../components/GraphView.vue'

const service = new GraphApi(api), route = useRoute()
const items = ref<GraphSummary[]>([]), detail = ref<GraphDetail | null>(null)
const loading = ref(false), error = ref('')
let generation = 0
async function load() {
  const current = ++generation
  detail.value = null; items.value = []; error.value = ''; loading.value = false
  if (!bearerToken.value) return
  loading.value = true
  try {
    const id = typeof route.params.id === 'string' ? route.params.id : ''
    if (id) { const value = await service.detail(id); if (current === generation) detail.value = value }
    else { const value = await service.list(); if (current === generation) items.value = value }
  } catch (e) { if (current === generation) error.value = agentError(e) }
  finally { if (current === generation) loading.value = false }
}
watch([bearerToken, () => route.params.id], () => { void load() }, { immediate: true })
const yes = (value: boolean) => value ? '是' : '否'
const entrypoints = (value: string[]) => value.map(v => ({ manual: '手动', schedule: '定时', synthetic_alert: '仅合成告警入口' }[v] ?? v)).join('、')
</script>
<template>
  <section class="workflow-page">
    <header v-if="!route.params.id" class="workflow-header"><div><p class="workflow-eyebrow">AGENT WORKFLOWS</p><h1>工作流</h1><p>了解每种工作流的用途、运行条件和执行过程。</p></div></header>
    <p v-if="!bearerToken" class="form-note">请先提供 Bearer Token。</p>
    <p v-else-if="loading">正在加载工作流…</p>
    <p v-else-if="error" role="alert">加载失败：{{ error }}</p>
    <template v-else-if="detail">
      <div class="workflow-detail-heading">
        <router-link class="workflow-back" to="/graphs">← 返回工作流</router-link>
        <p class="workflow-eyebrow">工作流详情</p>
        <div class="workflow-detail-title"><h2>{{ detail.title }}</h2><span class="workflow-card-status" :class="detail.status">{{ detail.status === 'recommended' ? '推荐' : '实验' }}</span></div>
        <p class="workflow-detail-summary">{{ detail.summary }}</p>
        <div class="workflow-detail-meta"><span>{{ entrypoints(detail.capabilities.entrypoints) }}</span><span>{{ detail.agents_using }} 个 Agent 使用</span><span v-if="detail.capabilities.uses_history">使用对话历史</span><span v-if="detail.capabilities.uses_tools">使用工具</span></div>
        <ul v-if="detail.notes.length" class="workflow-notes"><li v-for="note in detail.notes" :key="note">{{ note }}</li></ul>
      </div>
      <h3 class="workflow-section-title">执行结构</h3>
      <GraphView :nodes="detail.nodes" :edges="detail.edges" />
      <div class="workflow-info-grid">
        <section><h3>适用场景</h3><p>{{ detail.when_to_use }}</p></section>
        <section><h3>不适用场景</h3><p>{{ detail.when_not_to_use }}</p></section>
      </div>
      <section class="workflow-steps"><h3>执行过程</h3><ol><li v-for="step in detail.steps_text" :key="step">{{ step }}</li></ol></section>
      <details class="workflow-advanced"><summary>高级信息</summary><code>{{ detail.graph_id }}</code></details>
    </template>
    <template v-else-if="items.length">
      <div v-for="status in ['recommended', 'experimental']" :key="status" class="workflow-group"><div class="workflow-group-heading"><h2>{{ status === 'recommended' ? '推荐工作流' : '实验工作流' }}</h2><span>{{ items.filter(i => i.status === status).length }} 种</span></div><div class="workflow-cards">
        <router-link v-for="item in items.filter(i => i.status === status)" :key="item.graph_id" class="workflow-card" :to="{ name: 'graph-detail', params: { id: item.graph_id } }">
          <span class="workflow-card-status" :class="item.status">{{ item.status === 'recommended' ? '推荐' : '实验' }}</span>
          <h3>{{ item.title }}</h3><p class="workflow-card-summary">{{ item.summary }}</p>
          <div class="workflow-card-meta"><span>{{ entrypoints(item.capabilities.entrypoints) }}</span><span>{{ item.agents_using }} 个 Agent 使用</span></div>
          <p v-if="item.status === 'experimental'" class="workflow-card-warning">Worker 需显式启用。</p>
          <p v-if="item.graph_id === 'knowledge.citation_review'" class="workflow-card-warning">真实知识源和范围隔离尚未验收。</p>
          <p v-if="item.capabilities.entrypoints.includes('synthetic_alert')" class="workflow-card-warning">仅供本地集成测试。</p>
          <span class="workflow-card-action">查看详情 <span aria-hidden="true">→</span></span>
        </router-link>
      </div></div>
      <details class="workflow-compare"><summary>能力对比</summary><div class="graph-scroll"><table class="graph-table"><thead><tr><th>工作流</th><th>工具</th><th>历史</th><th>人工等待</th><th>知识工具</th><th>需提供步骤工作流</th><th>运行入口</th></tr></thead>
        <tbody><tr v-for="item in items" :key="item.graph_id"><td>{{ item.title }}</td><td>{{ yes(item.capabilities.uses_tools) }}</td><td>{{ yes(item.capabilities.uses_history) }}</td><td>{{ yes(item.capabilities.may_interrupt) }}</td><td>{{ yes(item.capabilities.needs_knowledge_tool) }}</td><td>{{ yes(item.capabilities.needs_workflow) }}</td><td>{{ entrypoints(item.capabilities.entrypoints) }}</td></tr></tbody></table></div></details>
    </template>
    <p v-else-if="bearerToken && !loading">暂无可用工作流。</p>
  </section>
</template>
