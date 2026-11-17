<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import { Plus, Refresh } from '@element-plus/icons-vue'
import { api, bearerToken } from '../session'
import { isRunActive, type Agent } from '../api'
import { listMessages, listThreads, submitConversation, type ThreadItem, type ThreadMessage } from '../threads-api'

const agents = ref<Agent[]>([]), agent = ref(''), threads = ref<ThreadItem[]>([])
const selected = ref<string | null>(null), messages = ref<ThreadMessage[]>([]), question = ref('')
const cursor = ref<string | null>(null), loading = ref(false), sending = ref(false), running = ref(false), error = ref('')
let agentGeneration = 0, listGeneration = 0, threadGeneration = 0, messageGeneration = 0
let timer: ReturnType<typeof setTimeout> | undefined, previous = '', key = ''

function stopPoll() { if (timer) clearTimeout(timer); timer = undefined }
function showError(e: unknown) {
  const message = e instanceof Error ? e.message : '请求失败。'
  error.value = message.includes('CONVERSATION_WORKFLOW_REQUIRED') ? '请先在 Agent 管理中发布 dod.v18 多轮问答工作流。'
    : message.includes('CONVERSATION_HISTORY_INCOMPATIBLE') ? '此会话包含不支持的旧工作流记录，请新建会话。'
    : message.includes('THREAD_BUSY') ? '会话中还有任务正在执行，请等待完成后再发送。' : message
}
async function loadAgentChoices() {
  const current = ++agentGeneration
  agent.value = ''; agents.value = []; error.value = ''
  if (!bearerToken.value) return
  try {
    const items = await api.agents()
    if (current !== agentGeneration) return
    agents.value = items
    if (items.length === 1) agent.value = items[0].agent_id
  } catch (e) { if (current === agentGeneration) showError(e) }
}
async function loadThreads(append = false) {
  const current = ++listGeneration, id = agent.value
  if (!append) { threads.value = []; cursor.value = null }
  if (!id || !bearerToken.value) { threads.value = []; cursor.value = null; loading.value = false; return }
  loading.value = true
  try {
    const page = await listThreads(api, id, append ? cursor.value ?? undefined : undefined)
    if (current !== listGeneration) return
    threads.value = append ? [...threads.value, ...page.items] : page.items
    cursor.value = page.next_cursor
  } catch (e) { if (current === listGeneration) showError(e) }
  finally { if (current === listGeneration) loading.value = false }
}
async function loadMessages(thread: string, generation: number) {
  const current = ++messageGeneration
  let after: string | undefined
  const seen = new Set<string>(), items: ThreadMessage[] = []
  do {
    const page = await listMessages(api, thread, after)
    if (generation !== threadGeneration || current !== messageGeneration) return
    items.push(...page.items)
    after = page.next_cursor ?? undefined
    if (after && seen.has(after)) throw new Error('会话分页游标重复。')
    if (after) seen.add(after)
  } while (after)
  messages.value = items
}
async function pollRun(runId: string, thread: string, generation: number) {
  try {
    const run = await api.run(runId)
    if (generation !== threadGeneration || thread !== selected.value) return
    running.value = isRunActive(run.status) || run.status === 'finalizing'
    if (running.value) timer = setTimeout(() => { void pollRun(runId, thread, generation) }, 2000)
    else {
      await loadMessages(thread, generation)
      if (generation !== threadGeneration || thread !== selected.value) return
      void loadThreads()
      if (!run.outcome) error.value = `任务状态：${run.status}`
    }
  } catch (e) { if (generation === threadGeneration) { running.value = false; showError(e) } }
}
async function openThread(item: ThreadItem) {
  stopPoll(); const current = ++threadGeneration
  selected.value = item.thread_id; messages.value = []; running.value = false; sending.value = false; error.value = ''
  try {
    await loadMessages(item.thread_id, current)
    if (current === threadGeneration && item.last_run_id && item.status && (isRunActive(item.status) || item.status === 'finalizing')) {
      running.value = true; void pollRun(item.last_run_id, item.thread_id, current)
    }
  } catch (e) { if (current === threadGeneration) showError(e) }
}
function newThread() { stopPoll(); threadGeneration++; messageGeneration++; selected.value = null; messages.value = []; running.value = false; sending.value = false; error.value = ''; previous = ''; key = '' }
async function submit() {
  const text = question.value.trim()
  if (!text || !agent.value || sending.value || running.value) return
  const signature = JSON.stringify([agent.value, selected.value, text])
  if (signature !== previous) { previous = signature; key = crypto.randomUUID() }
  sending.value = true; error.value = ''
  const current = threadGeneration
  try {
    const accepted = await submitConversation(api, agent.value, selected.value, text, key)
    if (current !== threadGeneration) return
    previous = ''; key = ''; selected.value = accepted.thread_id; question.value = ''; running.value = true
    void loadThreads()
    void pollRun(accepted.run_id, accepted.thread_id, current)
    await loadMessages(accepted.thread_id, current)
  } catch (e) { if (current === threadGeneration) showError(e) }
  finally { if (current === threadGeneration) sending.value = false }
}
watch(bearerToken, () => { newThread(); void loadAgentChoices() }, { immediate: true })
watch(agent, () => { newThread(); void loadThreads() }, { flush: 'sync' })
onUnmounted(() => { agentGeneration++; listGeneration++; threadGeneration++; messageGeneration++; stopPoll() })
</script>

<template>
  <div class="page-heading"><div><div class="eyebrow">AGENT <span></span> 会话</div><h1>会话</h1></div></div>
  <p v-if="!bearerToken" class="form-note">请先提供 Bearer Token。</p>
  <p v-if="error" class="form-error" role="alert">{{ error }}</p>
  <div class="thread-toolbar"><label>Agent <select v-model="agent" :disabled="!bearerToken"><option value="">请选择 Agent</option><option v-for="item in agents" :key="item.agent_id" :value="item.agent_id">{{ item.name }}</option></select></label>
    <button type="button" :disabled="!agent" title="刷新会话" aria-label="刷新会话" @click="loadThreads()"><Refresh /></button>
    <button type="button" :disabled="!agent" @click="newThread"><Plus /> 新会话</button></div>
  <div v-if="agent" class="thread-layout">
    <section class="thread-list" aria-label="会话列表"><h2>会话</h2><p v-if="loading">加载中...</p><p v-else-if="!threads.length">暂无会话。</p>
      <button v-for="item in threads" :key="item.thread_id" type="button" class="thread-row" :class="{ active: selected === item.thread_id }" @click="openThread(item)"><strong>{{ item.title || '未命名会话' }}</strong><small>{{ new Date(item.updated_at).toLocaleString() }}</small></button>
      <button v-if="cursor" type="button" :disabled="loading" @click="loadThreads(true)">加载更多</button>
    </section>
    <section class="thread-content"><h2>{{ selected ? '会话详情' : '新会话' }}</h2>
      <div v-if="messages.length" class="thread-messages"><article v-for="item in messages" :key="item.message_id" class="thread-message"><strong>{{ item.role === 'user' ? '我' : '助手' }}</strong><p>{{ item.input?.text ?? item.outcome?.summary ?? '' }}</p><router-link v-if="item.run_id" :to="`/runs/${item.run_id}`">任务详情</router-link></article></div>
      <p v-else class="form-note">暂无消息。</p>
      <p v-if="running" role="status">正在生成回答...</p>
      <form @submit.prevent="submit"><label>问题<textarea v-model="question" rows="3" :disabled="sending || running" required /></label><button class="primary-button" type="submit" :disabled="sending || running || !question.trim()">{{ sending ? '提交中...' : '发送' }}</button></form>
    </section>
  </div>
</template>

<style scoped>
.thread-toolbar { display:flex; flex-wrap:wrap; align-items:end; gap:10px; margin-bottom:20px; }
.thread-toolbar label, .thread-content form label { display:flex; flex-direction:column; gap:6px; font-size:13px; font-weight:600; }
.thread-toolbar select, .thread-content textarea { padding:9px; border:1px solid #cbd5d8; border-radius:4px; background:#fff; }
.thread-toolbar button { display:inline-flex; align-items:center; gap:6px; }.thread-toolbar svg { width:16px; height:16px; }
.thread-layout { display:grid; grid-template-columns:minmax(200px,280px) minmax(0,1fr); gap:28px; border-top:1px solid #dce2e5; padding-top:20px; }
.thread-layout h2 { font-size:16px; }.thread-row { display:grid; width:100%; text-align:left; gap:5px; padding:12px; border:0; border-bottom:1px solid #e4e9eb; border-radius:0; background:transparent; overflow-wrap:anywhere; }
.thread-row.active { background:#e5f1ed; border-left:3px solid #24765e; }.thread-row small { color:#59666a; }
.thread-content { min-width:0; max-width:900px; }.thread-messages { display:grid; gap:16px; margin:20px 0; }.thread-message { border-bottom:1px solid #e4e9eb; padding-bottom:14px; }.thread-message p { white-space:pre-wrap; overflow-wrap:anywhere; }.thread-message a { font-size:12px; }
.thread-content form { display:grid; gap:12px; }.thread-content textarea { width:100%; resize:vertical; }.thread-content form button { justify-self:start; }
@media (max-width:700px) { .thread-layout { grid-template-columns:1fr; } }
</style>
