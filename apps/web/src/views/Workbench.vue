<script setup lang="ts">
import { ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { api, bearerToken } from '../session'
import { createQaSubmission, missingQaFields, validQaWindow, type QaMode, type QaRequest, type Agent } from '../api'

const router = useRouter()
const agents = ref<Agent[]>([])
const agent = ref(''), question = ref(''), service = ref(''), region = ref(''), environment = ref('test')
const qaMode = ref<QaMode>('llm')
const start = ref(''), end = ref('')
const loadingAgents = ref(false), submitting = ref(false), error = ref('')
let generation = 0
let submitQa = createQaSubmission(api)

async function loadAgents() {
  const current = ++generation
  agents.value = []; agent.value = ''; error.value = ''
  if (!bearerToken.value) { loadingAgents.value = false; return }
  loadingAgents.value = true
  try {
    const result = await api.agents()
    if (current !== generation) return
    agents.value = result
    if (result.length === 1) agent.value = result[0].agent_id
  } catch (e) { if (current === generation) error.value = e instanceof Error ? e.message : '加载 Agent 失败' }
  finally { if (current === generation) loadingAgents.value = false }
}
watch(bearerToken, () => { submitQa = createQaSubmission(api); void loadAgents() }, { immediate: true })

async function submit() {
  error.value = ''
  const missing = missingQaFields({ agent: agent.value, question: question.value, service: service.value,
    region: region.value, start: start.value, end: end.value }, qaMode.value)
  if (missing.length) { error.value = `请填写：${missing.join('、')}。`; return }
  const request: QaRequest = { agent_id: agent.value, text: question.value.trim() }
  if (qaMode.value === 'knowledge') {
    const from = new Date(start.value), to = new Date(end.value)
    if (!validQaWindow(from, to)) { error.value = '结束时间必须晚于开始时间，且查询范围不能超过一小时。'; return }
    request.scope = { service: service.value.trim(), environment: environment.value, region: region.value.trim(), start_at: from.toISOString(), end_at: to.toISOString() }
  }
  submitting.value = true
  try {
    const accepted = await submitQa(request)
    await router.push({ name: 'run-detail', params: { id: accepted.run_id } })
  } catch (e) { error.value = e instanceof Error ? e.message : '提交失败，请重试。' }
  finally { submitting.value = false }
}
</script>

<template>
  <div class="page-heading"><div><div class="eyebrow">WORKBENCH <span></span> 工作台</div><h1>问答</h1></div></div>
  <section class="panel qa-panel">
    <form class="qa-form" novalidate @submit.prevent="submit">
      <p v-if="error" class="form-error" role="alert">{{ error }}</p>
      <label>Agent<select v-model="agent" required :disabled="loadingAgents || submitting || !bearerToken"><option value="">{{ loadingAgents ? '加载中…' : '请选择 Agent' }}</option><option v-for="item in agents" :key="item.agent_id" :value="item.agent_id">{{ item.name }} ({{ item.agent_id }})</option></select></label>
      <p v-if="bearerToken && !loadingAgents && !agents.length && !error" class="form-note">没有可访问的 Agent。</p>
      <fieldset class="qa-mode"><legend>问答方式</legend><div class="mode-options">
        <label><input v-model="qaMode" type="radio" value="llm" />纯 LLM 问答</label>
        <label><input v-model="qaMode" type="radio" value="knowledge" />知识问答</label>
      </div></fieldset>
      <p class="form-note">问答方式须与 Agent 已发布的工作流一致。</p>
      <label>问题<textarea v-model="question" rows="5" required placeholder="输入你的问题"></textarea></label>
      <fieldset v-if="qaMode === 'knowledge'"><legend>查询范围</legend><div class="scope-grid">
        <label>服务<input v-model="service" required placeholder="服务名称" /></label>
        <label>环境<select v-model="environment"><option value="test">test</option><option value="uat">uat</option><option value="live">live</option></select></label>
        <label>区域<input v-model="region" required placeholder="区域" /></label>
        <label>开始时间<input v-model="start" type="datetime-local" required /></label>
        <label>结束时间<input v-model="end" type="datetime-local" required /></label>
      </div><p class="form-note">时间按设备本地时区输入，最长一小时；查询范围必须符合已授权的配置。</p></fieldset>
      <div class="form-actions"><button class="primary-button" type="submit" :disabled="!bearerToken || submitting || loadingAgents">{{ submitting ? '提交中…' : '提交问题' }}</button><button v-if="bearerToken && !loadingAgents" type="button" :disabled="submitting" @click="loadAgents">刷新 Agent</button></div>
    </form>
  </section>
</template>
