<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import { api, bearerToken } from '../session'
import type { Agent } from '../api'
import { ToolApi, type ToolBinding, type ToolConnection, type ToolSummary } from '../tools-api'

const agents = ref<Agent[]>([]), agent = ref('')
const connections = ref<ToolConnection[]>([]), summaries = ref<ToolSummary[]>([])
const selected = ref<ToolSummary | null>(null)
const draft = ref<ToolBinding | null>(null), revision = ref<number | null>(null)
interface DiscoveredTool {name: string; description?: string; schema: Record<string, unknown>}
const discovered = ref<DiscoveredTool[]>([])
const loading = ref(false), busy = ref(false), error = ref(''), notice = ref('')
let generation = 0, pollTimer: ReturnType<typeof setTimeout> | undefined
const service = new ToolApi(api)

const compatible = (tool: string) =>
  tool === 'knowledge_search' ||
  connections.value.some(item => item.tool_conversation_tools.includes(tool) &&
    item.mapping_policies.some(policy => policy.tool_name === tool))
function stopPoll() { if (pollTimer) clearTimeout(pollTimer); pollTimer = undefined }

async function loadAgents() {
  agents.value = bearerToken.value ? await api.agents() : []
  if (!agents.value.some(item => item.agent_id === agent.value)) {
    agent.value = agents.value[0]?.agent_id ?? ''
  }
}
async function load() {
  const current = ++generation
  stopPoll()
  connections.value = []; summaries.value = []; selected.value = null; draft.value = null
  revision.value = null; discovered.value = []; error.value = ''; notice.value = ''
  if (!agent.value || !bearerToken.value) return
  loading.value = true
  try {
    const [connectionPage, bindingPage] = await Promise.all([
      service.connections(agent.value), service.bindings(agent.value),
    ])
    if (current !== generation) return
    connections.value = connectionPage.items
    summaries.value = bindingPage.items
  } catch (e) {
    if (current === generation) error.value = e instanceof Error ? e.message : '工具配置加载失败'
  } finally {
    if (current === generation) loading.value = false
  }
}
async function open(summary: ToolSummary) {
  selected.value = summary; draft.value = null; revision.value = null; error.value = ''; notice.value = ''
  try {
    const result = await service.binding(agent.value, summary.alias) as {
      draft: ToolBinding | null; revision: number | null
    }
    draft.value = result.draft ? structuredClone(result.draft) : null
    revision.value = result.revision
  } catch (e) {
    error.value = e instanceof Error ? e.message : '工具版本加载失败'
  }
}
async function discover(connection: ToolConnection) {
  if (!compatibleTool(connection)) return
  const current = ++generation
  busy.value = true; error.value = ''
  try {
    const job = await service.discover(agent.value, connection.server, connection.credential_scope) as {
      job_id: string
    }
    await waitJob(job.job_id, current)
  } catch (e) {
    if (current === generation) error.value = e instanceof Error ? e.message : '发现失败'
  } finally {
    if (current === generation) busy.value = false
  }
}
function compatibleTool(connection: ToolConnection) {
  return connection.read_tools.some(compatible)
}
async function waitJob(id: string, current: number) {
  for (let attempt = 0; attempt < 60; attempt++) {
    if (current !== generation) return
    const job = await service.job(agent.value, id)
    if (job.status === 'succeeded') {
      const result = job.result_ref ? JSON.parse(job.result_ref) : {tools: []}
      discovered.value = (result.tools ?? []).filter((tool: {name?: string; tool_conversation_compatible?: boolean}) =>
        tool.tool_conversation_compatible === true)
      notice.value = discovered.value.length ? '发现完成。' : '没有与工具对话兼容的已授权工具。'
      return
    }
    if (['failed', 'cancelled'].includes(job.status)) throw new Error(`发现失败：${job.status}`)
    await new Promise(resolve => { pollTimer = setTimeout(resolve, 1000) })
  }
  throw new Error('发现任务超时')
}
function makeDraft() {
  const tool = discovered.value[0]
  const connection = connections.value.find(item => item.read_tools.includes(tool.name))
  if (!tool || !connection) return
  const schema = tool.schema ?? {type: 'object'}
  draft.value = {
    alias: tool.name === 'knowledge_search' ? 'knowledge.search' : tool.name.replace(/[^A-Za-z0-9_-]/g, '_'),
    provider: 'mcp',
    tool_name: tool.name,
    server: connection.server,
    credential_scope: connection.credential_scope,
    permission: tool.name === 'knowledge_search' ? 'knowledge.read' : `${tool.name}.read`,
    input_schema: schema,
    output_schema: null,
    timeout_seconds: 10,
    max_output_bytes: 65536,
    read_only: true,
  }
  revision.value = revision.value ?? 0
}
async function save() {
  if (!draft.value || revision.value === null) return
  busy.value = true; error.value = ''
  try {
    const result = await service.save(agent.value, draft.value, revision.value) as {
      revision: number
    }
    revision.value = result.revision
    notice.value = '绑定草稿已保存。'
    const page = await service.bindings(agent.value)
    summaries.value = page.items
  } catch (e) {
    error.value = e instanceof Error ? e.message : '保存失败'
  } finally {
    busy.value = false
  }
}
async function publish() {
  if (!draft.value || revision.value === null) return
  busy.value = true; error.value = ''
  try {
    await service.publish(agent.value, draft.value.alias, revision.value)
    notice.value = '只读工具版本已发布；还需要在 Agent 配置中引用并发布配置。'
    await load()
  } catch (e) {
    error.value = e instanceof Error ? e.message : '发布失败'
  } finally {
    busy.value = false
  }
}
watch(bearerToken, () => { generation++; stopPoll(); agent.value = ''; void loadAgents().then(load) }, {immediate: true})
watch(agent, load)
onUnmounted(() => { generation++; stopPoll() })
</script>

<template>
  <div class="page-heading"><div><div class="eyebrow">TOOLS <span></span> 工具连接</div><h1>只读 MCP 工具</h1><p>连接、授权白名单、参数映射策略和凭据作用域来自部署配置。</p></div><label class="tool-agent">Agent<select v-model="agent"><option v-for="item in agents" :key="item.agent_id" :value="item.agent_id">{{ item.name }}</option></select></label></div>
  <p v-if="error" class="form-error" role="alert">{{ error }}</p>
  <p v-if="notice" class="form-note" role="status">{{ notice }}</p>
  <p v-if="loading" class="form-note">正在加载工具配置…</p>
  <div v-else class="tool-layout">
    <section class="panel"><h2>授权连接</h2><p v-if="!connections.length">当前 Agent 没有已授权连接。</p><article v-for="connection in connections" :key="`${connection.server}:${connection.credential_scope}`" class="tool-row"><div><strong>{{ connection.server }}</strong><small>{{ connection.credential_scope }} · {{ connection.status }}</small><p>{{ connection.read_tools.join(', ') }}</p><p v-if="connection.mapping_policies.length" class="muted">固定映射：{{ connection.mapping_policies.map(policy => policy.tool_name).join(', ') }} · 模型仅可填写声明字段，范围与身份由部署配置注入。</p><p v-if="!connection.read_tools.every(compatible)" class="muted">其他只读工具保留管理展示，未配置映射策略时不能进入工具对话。</p></div><button type="button" :disabled="busy || !compatibleTool(connection)" @click="discover(connection)">获取工具</button></article></section>
    <section class="panel"><h2>绑定</h2><p v-if="!summaries.length">尚未保存工具绑定。</p><button v-for="item in summaries" :key="item.alias" class="binding-row" @click="open(item)"><strong>{{ item.alias }}</strong><small>草稿 rev {{ item.draft_revision ?? '—' }} · 最新版本 {{ item.latest_ref?.version ?? '—' }}</small></button></section>
    <section v-if="draft || discovered.length" class="panel tool-editor"><h2>配置绑定</h2><div v-if="discovered.length" class="tool-picker"><label>已发现工具<select><option v-for="tool in discovered" :key="tool.name">{{ tool.name }}</option></select></label><button type="button" @click="makeDraft">生成草稿</button></div><template v-if="draft"><dl><div><dt>Alias</dt><dd>{{ draft.alias }}</dd></div><div><dt>Tool</dt><dd>{{ draft.tool_name }}</dd></div><div><dt>权限</dt><dd>{{ draft.permission }}</dd></div><div><dt>只读</dt><dd>{{ draft.read_only ? '是' : '否' }}</dd></div><div><dt>超时</dt><dd>{{ draft.timeout_seconds }} 秒</dd></div></dl><details><summary>完整 Schema</summary><pre>{{ JSON.stringify(draft.input_schema, null, 2) }}</pre></details><div class="tool-actions"><button type="button" :disabled="busy" @click="save">保存草稿</button><button type="button" :disabled="busy || revision === null || !revision" @click="publish">发布只读版本</button></div><p class="muted">绑定发布后仍需在 Agent 配置中选择版本、保存并发布配置才会生效。</p></template></section>
  </div>
</template>

<style scoped>
.tool-agent { display: grid; gap: 6px; }
.tool-layout { display: grid; grid-template-columns: minmax(0, 1.4fr) minmax(260px, .8fr); gap: 16px; align-items: start; }
.tool-layout > .panel { padding: 18px; }
.tool-editor { grid-column: 1 / -1; }
.tool-row { display: flex; gap: 16px; justify-content: space-between; border-top: 1px solid #e5eaee; padding: 14px 0; }
.tool-row small, .tool-row p, .muted { color: #6c7b85; }
.binding-row { display: grid; width: 100%; text-align: left; margin-bottom: 8px; }
.binding-row small { color: #6c7b85; }
.tool-picker, .tool-actions { display: flex; gap: 10px; align-items: end; flex-wrap: wrap; }
.tool-picker label { display: grid; gap: 6px; flex: 1; }
.tool-editor dl { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.tool-editor dt { color: #71818d; }.tool-editor dd { margin: 0; }
.tool-editor pre { overflow: auto; max-height: 280px; }
@media (max-width: 800px) { .tool-layout, .tool-editor dl { grid-template-columns: 1fr; } }
</style>
