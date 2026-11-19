<script setup lang="ts">
import { computed, inject, onUnmounted, reactive, ref, watch, watchEffect } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowUp, MagicStick, Reading, Refresh } from '@element-plus/icons-vue'
import { api, bearerToken } from '../session'
import { createQaSubmission, isRunActive, missingQaFields, validQaWindow, type QaRequest, type Agent } from '../api'
import { listMessages, listThreads, submitConversation, type ThreadItem, type ThreadMessage } from '../threads-api'
import { chatSidebarKey, emptyChatSidebar } from '../chat-sidebar'
import KnowledgeAnswer from '../components/KnowledgeAnswer.vue'

const router = useRouter(), sidebar = inject(chatSidebarKey, reactive(emptyChatSidebar()))
const agents = ref<Agent[]>([]), agent = ref(''), question = ref('')
const mode = ref<'conversation' | 'single' | 'knowledge'>('conversation')
const service = ref(''), region = ref(''), environment = ref('test'), start = ref(''), end = ref('')
const threads = ref<ThreadItem[]>([]), selected = ref<string | null>(null), messages = ref<ThreadMessage[]>([]), cursor = ref<string | null>(null)
const loadingAgents = ref(false), loadingThreads = ref(false), submitting = ref(false), running = ref(false), error = ref('')
const runId = ref(''), runStatus = ref('')
const selectedAgent = computed(() => agents.value.find(item => item.agent_id === agent.value)?.name || '助手')
let agentGeneration = 0, listGeneration = 0, threadGeneration = 0, messageGeneration = 0
let timer: ReturnType<typeof setTimeout> | undefined, previous = '', key = ''
let submitQa = createQaSubmission(api)

function stopPoll() { if (timer) clearTimeout(timer); timer = undefined }
function showError(e: unknown) {
  const message = e instanceof Error ? e.message : '请求失败，请重试。'
  error.value = message.includes('CONVERSATION_WORKFLOW_REQUIRED') ? '当前 Agent 未绑定多轮对话工作流，请选择“单次问答”或在 Agent 管理中发布多轮对话工作流。'
    : message.includes('CONVERSATION_HISTORY_INCOMPATIBLE') ? '此会话包含不兼容的历史记录，请新建对话。'
    : message.includes('THREAD_BUSY') ? '会话中还有任务正在执行，请等待完成后再发送。' : message
}
function resetThread(force = false) {
  if (submitting.value && !force) return
  stopPoll(); threadGeneration++; messageGeneration++
  selected.value = null; messages.value = []; running.value = false; submitting.value = false
  runId.value = ''; runStatus.value = ''; error.value = ''; previous = ''; key = ''
}
function newThread() { resetThread(); if (!submitting.value) question.value = '' }
async function loadAgents() {
  if (submitting.value) return
  const current = ++agentGeneration, old = agent.value
  agents.value = []; error.value = ''
  if (!bearerToken.value) { agent.value = ''; loadingAgents.value = false; return }
  loadingAgents.value = true
  try {
    const result = await api.agents()
    if (current !== agentGeneration) return
    agents.value = result
    agent.value = result.some(item => item.agent_id === old) ? old : result.length === 1 ? result[0].agent_id : ''
  } catch (e) { if (current === agentGeneration) { agent.value = ''; showError(e) } }
  finally { if (current === agentGeneration) loadingAgents.value = false }
}
async function loadThreads(append = false) {
  const current = ++listGeneration, id = agent.value
  if (!append) { threads.value = []; cursor.value = null }
  if (!id || !bearerToken.value) { threads.value = []; cursor.value = null; loadingThreads.value = false; return }
  loadingThreads.value = true
  try {
    const page = await listThreads(api, id, append ? cursor.value ?? undefined : undefined)
    if (current !== listGeneration) return
    threads.value = append ? [...threads.value, ...page.items] : page.items; cursor.value = page.next_cursor
  } catch (e) { if (current === listGeneration) showError(e) }
  finally { if (current === listGeneration) loadingThreads.value = false }
}
async function loadMessages(thread: string, generation: number) {
  const current = ++messageGeneration, items: ThreadMessage[] = [], seen = new Set<string>()
  let after: string | undefined
  do {
    const page = await listMessages(api, thread, after)
    if (generation !== threadGeneration || current !== messageGeneration) return
    items.push(...page.items); after = page.next_cursor ?? undefined
    if (after && seen.has(after)) throw new Error('会话分页游标重复。')
    if (after) seen.add(after)
  } while (after)
  messages.value = items
}
async function pollRun(id: string, thread: string, generation: number) {
  try {
    const run = await api.run(id)
    if (generation !== threadGeneration || thread !== selected.value) return
    runId.value = id; runStatus.value = run.status; running.value = isRunActive(run.status)
    if (running.value) timer = setTimeout(() => { void pollRun(id, thread, generation) }, 2000)
    else {
      await loadMessages(thread, generation)
      if (generation !== threadGeneration || thread !== selected.value) return
      void loadThreads()
      if (!['succeeded', 'partial'].includes(run.status) || !run.outcome) error.value = `任务状态：${run.status}。请查看任务详情。`
    }
  } catch (e) { if (generation === threadGeneration) { running.value = false; showError(e) } }
}
async function openThread(item: ThreadItem) {
  if (submitting.value) return
  stopPoll(); const current = ++threadGeneration
  selected.value = item.thread_id; messages.value = []; running.value = false; error.value = ''
  runId.value = item.last_run_id || ''; runStatus.value = item.status || ''
  try {
    await loadMessages(item.thread_id, current)
    if (current === threadGeneration && item.last_run_id && item.status && isRunActive(item.status)) {
      running.value = true; void pollRun(item.last_run_id, item.thread_id, current)
    } else if (current === threadGeneration && item.last_run_id && item.status && !['succeeded', 'partial'].includes(item.status)) {
      error.value = `任务状态：${item.status}。请查看任务详情。`
    }
  } catch (e) { if (current === threadGeneration) showError(e) }
}
async function submit() {
  if (submitting.value || running.value || loadingAgents.value || !bearerToken.value) return
  error.value = ''
  const missing = missingQaFields({ agent: agent.value, question: question.value, service: service.value,
    region: region.value, start: start.value, end: end.value }, mode.value === 'knowledge' ? 'knowledge' : 'llm')
  if (missing.length) { error.value = `请填写：${missing.join('、')}。`; return }
  const text = question.value.trim(), current = threadGeneration
  if (mode.value !== 'conversation') {
    const request: QaRequest = { agent_id: agent.value, text }
    if (mode.value === 'knowledge') {
      const from = new Date(start.value), to = new Date(end.value)
      if (!validQaWindow(from, to)) { error.value = '结束时间必须晚于开始时间，且查询范围不能超过一小时。'; return }
      request.scope = { service: service.value.trim(), environment: environment.value, region: region.value.trim(), start_at: from.toISOString(), end_at: to.toISOString() }
    }
    submitting.value = true
    try { const accepted = await submitQa(request); if (current === threadGeneration) await router.push({ name: 'run-detail', params: { id: accepted.run_id } }) }
    catch (e) { if (current === threadGeneration) showError(e) }
    finally { if (current === threadGeneration) submitting.value = false }
    return
  }
  const signature = JSON.stringify([agent.value, selected.value, text])
  if (signature !== previous) { previous = signature; key = crypto.randomUUID() }
  submitting.value = true
  try {
    const accepted = await submitConversation(api, agent.value, selected.value, text, key)
    if (current !== threadGeneration) return
    previous = ''; key = ''; selected.value = accepted.thread_id; question.value = ''; running.value = true
    runId.value = accepted.run_id; runStatus.value = 'queued'
    void loadThreads(); void pollRun(accepted.run_id, accepted.thread_id, current)
    await loadMessages(accepted.thread_id, current)
  } catch (e) { if (current === threadGeneration) showError(e) }
  finally { if (current === threadGeneration) submitting.value = false }
}
function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); void submit() }
}
watch(bearerToken, () => {
  resetThread(true); listGeneration++; threads.value = []; cursor.value = null; agent.value = ''; question.value = ''
  submitQa = createQaSubmission(api); void loadAgents()
}, { immediate: true, flush: 'sync' })
watch(agent, () => { resetThread(true); void loadThreads() }, { flush: 'sync' })
watchEffect(() => { sidebar.threads = threads.value; sidebar.selected = selected.value; sidebar.agent = agent.value; sidebar.loading = loadingThreads.value; sidebar.hasMore = !!cursor.value })
sidebar.newThread = newThread; sidebar.openThread = item => { void openThread(item) }; sidebar.loadMore = () => { void loadThreads(true) }
onUnmounted(() => { agentGeneration++; listGeneration++; threadGeneration++; messageGeneration++; stopPoll(); Object.assign(sidebar, emptyChatSidebar()) })
</script>

<template>
  <section class="chat-page" aria-label="工作台">
    <div v-if="!selected && !messages.length" class="chat-intro"><span>你的工作，从这里开始</span><h1>今天，想解决什么问题？</h1><p>选一位合适的 Agent，把问题交给它。</p></div>
    <div v-else class="chat-thread" aria-label="对话消息" aria-live="polite">
      <p v-if="selected && !messages.length && !running" class="chat-empty">暂无消息。</p>
      <article v-for="item in messages" :key="item.message_id" :class="item.role === 'user' ? 'chat-user' : 'chat-assistant'">
        <template v-if="item.role === 'user'">{{ item.input?.text || '' }}</template>
        <template v-else><div class="assistant-heading"><MagicStick /> {{ selectedAgent }}</div><KnowledgeAnswer v-if="item.outcome?.knowledge_answer && item.run_id" :run-id="item.run_id" :answer="item.outcome.knowledge_answer" /><p v-else>{{ item.outcome?.summary || '' }}</p><router-link v-if="item.run_id" :to="`/runs/${item.run_id}`">任务详情 ↗</router-link></template>
      </article>
      <div v-if="running" class="chat-status" role="status">{{ runStatus === 'waiting_input' ? '等待补充信息' : runStatus === 'waiting_approval' ? '等待审批' : '正在生成回答…' }} <router-link v-if="runId" :to="`/runs/${runId}`">查看任务详情 ↗</router-link></div>
      <router-link v-else-if="runId && error" class="chat-status" :to="`/runs/${runId}`">查看任务详情 ↗</router-link>
    </div>
    <div class="composer-wrap">
      <p v-if="!bearerToken" class="chat-notice">请先在身份凭据中提供 Bearer Token。</p>
      <p v-if="bearerToken && !loadingAgents && !agents.length && !error" class="chat-notice">没有可访问的 Agent，请先在 Agent 管理中配置。</p>
      <p v-if="error" class="form-error" role="alert">{{ error }}</p>
      <fieldset v-if="mode === 'knowledge'" class="chat-scope"><legend>知识查询范围</legend><div class="chat-scope-grid">
        <label>服务<input v-model="service" placeholder="服务名称" /></label><label>环境<select v-model="environment"><option value="test">test</option><option value="uat">uat</option><option value="live">live</option></select></label><label>区域<input v-model="region" placeholder="区域" /></label>
        <label>开始时间<input v-model="start" type="datetime-local" /></label><label>结束时间<input v-model="end" type="datetime-local" /></label>
      </div><p>时间按设备本地时区输入，最长一小时；查询范围必须符合授权配置。</p></fieldset>
      <form class="chat-composer" novalidate @submit.prevent="submit">
        <label class="sr-only" for="chat-question">问题</label><textarea id="chat-question" v-model="question" rows="3" :disabled="submitting || running" placeholder="向 Agent 提问，或描述你想完成的任务…" @keydown="onKeydown"></textarea>
        <div class="composer-toolbar"><div class="mode-switch" aria-label="问答方式"><button type="button" :aria-pressed="mode === 'conversation'" :disabled="submitting || running" @click="mode = 'conversation'">多轮对话</button><button type="button" :aria-pressed="mode === 'single'" :disabled="submitting || running" @click="mode = 'single'">单次问答</button><button type="button" :aria-pressed="mode === 'knowledge'" :disabled="submitting || running" @click="mode = 'knowledge'"><Reading /> 知识问答</button></div>
          <label class="agent-picker"><span class="sr-only">选择 Agent</span><select v-model="agent" :disabled="loadingAgents || submitting || !bearerToken"><option value="">{{ loadingAgents ? '加载中…' : '请选择 Agent' }}</option><option v-for="item in agents" :key="item.agent_id" :value="item.agent_id">{{ item.name }}</option></select></label>
          <button class="chat-refresh" type="button" aria-label="刷新 Agent" title="刷新 Agent" :disabled="submitting || loadingAgents || !bearerToken" @click="loadAgents"><Refresh /></button>
          <button class="chat-send" type="submit" aria-label="发送问题" :disabled="!bearerToken || submitting || running || loadingAgents || !question.trim()"><ArrowUp /></button>
        </div>
      </form>
      <div v-if="!selected && !messages.length && !question.trim()" class="chat-suggestions" aria-label="问题示例">
        <button type="button" :disabled="submitting || running" @click="mode = 'knowledge'; question = '帮我整理相关知识的要点。'">整理一份知识摘要</button>
        <button type="button" :disabled="submitting || running" @click="question = '帮我梳理问题的排查思路。'">梳理问题排查思路</button>
        <button type="button" :disabled="submitting || running" @click="question = '帮我写一段清晰的回复。'">写一段清晰的回复</button>
      </div>
      <p class="chat-footnote">{{ mode === 'conversation' ? '多轮对话需要 Agent 绑定对话（多轮）工作流。' : mode === 'single' ? '单次问答不使用会话历史，结果在任务详情中显示。' : '知识问答需要知识问答工作流，将新建任务并在任务详情中显示结果。' }} Enter 发送 · Shift + Enter 换行</p>
    </div>
  </section>
</template>
