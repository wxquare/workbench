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
  <section>
    <header class="page-header"><div><h1>流程图目录</h1><p>查看内置流程的用途、能力和执行结构。</p></div></header>
    <p v-if="!bearerToken" class="form-note">请先提供 Bearer Token。</p>
    <p v-else-if="loading">正在加载流程图…</p>
    <p v-else-if="error" role="alert">加载失败：{{ error }}</p>
    <template v-else-if="detail">
      <router-link to="/graphs">返回流程图目录</router-link>
      <h2>{{ detail.title }} <small>{{ detail.status === 'recommended' ? '推荐' : '实验' }}</small></h2>
      <p>{{ detail.summary }} · {{ detail.agents_using }} 个 Agent 使用</p>
      <p><strong>适用：</strong>{{ detail.when_to_use }}</p><p><strong>不适用：</strong>{{ detail.when_not_to_use }}</p>
      <p><strong>运行入口：</strong>{{ entrypoints(detail.capabilities.entrypoints) }}</p>
      <ul v-if="detail.notes.length"><li v-for="note in detail.notes" :key="note">{{ note }}</li></ul>
      <GraphView :nodes="detail.nodes" :edges="detail.edges" />
      <h3>执行过程</h3><ol><li v-for="step in detail.steps_text" :key="step">{{ step }}</li></ol>
      <details><summary>高级信息</summary><code>{{ detail.graph_id }}</code></details>
    </template>
    <template v-else-if="items.length">
      <h2>能力对比</h2><div class="graph-scroll"><table class="graph-table"><thead><tr><th>流程图</th><th>工具</th><th>历史</th><th>人工等待</th><th>知识工具</th><th>工作流</th><th>运行入口</th></tr></thead>
        <tbody><tr v-for="item in items" :key="item.graph_id"><td>{{ item.title }}</td><td>{{ yes(item.capabilities.uses_tools) }}</td><td>{{ yes(item.capabilities.uses_history) }}</td><td>{{ yes(item.capabilities.may_interrupt) }}</td><td>{{ yes(item.capabilities.needs_knowledge_tool) }}</td><td>{{ yes(item.capabilities.needs_workflow) }}</td><td>{{ entrypoints(item.capabilities.entrypoints) }}</td></tr></tbody></table></div>
      <div v-for="status in ['recommended', 'experimental']" :key="status"><h2>{{ status === 'recommended' ? '推荐' : '实验' }}</h2><div class="graph-cards">
        <article v-for="item in items.filter(i => i.status === status)" :key="item.graph_id" class="graph-card"><h3><router-link :to="{ name: 'graph-detail', params: { id: item.graph_id } }">{{ item.title }}</router-link></h3><p>{{ item.summary }}</p><p>{{ entrypoints(item.capabilities.entrypoints) }} · {{ item.agents_using }} 个 Agent 使用</p><p v-if="item.graph_id === 'knowledge.citation_review'">真实知识源和范围隔离尚未验收。</p><p v-if="item.capabilities.entrypoints.includes('synthetic_alert')">仅供本地集成测试。</p></article>
      </div></div>
    </template>
    <p v-else-if="bearerToken && !loading">暂无可用流程图。</p>
  </section>
</template>
