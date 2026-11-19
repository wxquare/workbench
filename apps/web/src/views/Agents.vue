<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { Plus, Delete, Close } from '@element-plus/icons-vue'
import { ApiError, type Agent } from '../api'
import { api, bearerToken } from '../session'
import { AgentApi, parseAgentDraft, agentError, type AvailableModel, type Draft, type ModelRef, type AgentDetail } from '../agents-api'
import { SkillApi, type Skill } from '../skills-api'
import { GraphApi, type GraphSummary } from '../graphs-api'

const items = ref<Agent[]>([])
const selected = ref<AgentDetail | null>(null)
const editor = ref(''), baseline = ref(''), name = ref('')
const error = ref(''), notice = ref('')
const models = ref<AvailableModel[]>([]), modelError = ref(''), modelsLoading = ref(false)
const skills = ref<Skill[]>([]), skillError = ref(''), skillsLoading = ref(false)
const skillDialogOpen = ref(false), skillSearch = ref(''), pendingSkills = ref<string[]>([])
const skillSearchInput = ref<HTMLInputElement | null>(null), skillAddButton = ref<HTMLButtonElement | null>(null)
const skillDialog = ref<HTMLElement | null>(null)
const skillService = new SkillApi(api)
const graphService = new GraphApi(api)
const graphs = ref<GraphSummary[]>([]), graphError = ref(''), graphsLoading = ref(false)
let graphGeneration = 0
const loading = ref(false), busy = ref(false), stale = ref(false)
const invalidNumbers = ref<Record<string, string>>({})
const dirty = computed(() => editor.value !== baseline.value || Object.keys(invalidNumbers.value).length > 0)
const currentDraft = computed(() => {
  if (!selected.value?.draft) return null
  try { return parseAgentDraft(editor.value, selected.value.draft) } catch { return null }
})
const budgetFields = [
  { key: 'wall_seconds', label: '执行时间上限（秒）' },
  { key: 'model_calls', label: '模型调用上限' },
  { key: 'tool_calls', label: '工具调用上限' },
  { key: 'tokens', label: 'Token 上限' },
] as const
const validationError = computed(() => {
  const draft = currentDraft.value
  if (graphsLoading.value || graphError.value || !graphs.value.length) return '请先加载工作流目录。'
  if (draft && !graphs.value.some(graph => graph.graph_id === draft.graph_id)) return '当前工作流不可用，请重新选择。'
  if (!draft) return '请先修正配置 JSON。'
  if (Object.keys(invalidNumbers.value).length) return '预算和知识有效期须为正整数，对话历史轮数须为 0 至 8 的整数。'
  const age = draft.knowledge_max_age_seconds
  if (age != null && (!Number.isSafeInteger(age) || (age as number) <= 0)) return '知识来源有效期须为正整数。'
  for (const [key, max] of [['description', 1000], ['system_prompt', 12000]] as const) {
    const value = draft[key]
    if (value !== undefined && (typeof value !== 'string' || Array.from(value).length > max)) return `${key} 须为不超过 ${max} 个字符的字符串。`
  }
  if (draft.response_language !== undefined && !['auto', 'zh', 'en'].includes(draft.response_language as string)) return '回复语言无效。'
  if (draft.response_format !== undefined && !['default', 'markdown', 'json'].includes(draft.response_format as string)) return '回复格式无效。'
  const history = draft.conversation_history_turns
  if (history !== undefined && (!Number.isInteger(history) || (history as number) < 0 || (history as number) > 8)) return '对话历史轮数须为 0 至 8 的整数。'
  const budget = draft.budget_limits
  if (budget != null && (typeof budget !== 'object' || Array.isArray(budget))) return '预算上限须为对象或 null。'
  if (budget && typeof budget === 'object') {
    for (const { key } of budgetFields) {
      const value = (budget as Draft)[key]
      if (value != null && (!Number.isSafeInteger(value) || (value as number) <= 0)) return '预算上限须为正整数。'
    }
  }
  return ''
})
const workflow = computed(() => {
  if (!selected.value?.draft) return ''
  try {
    const value = parseAgentDraft(editor.value, selected.value.draft).graph_id
    return typeof value === 'string' ? value : 'conversation.multi_turn'
  } catch { return '' }
})
const selectedGraph = computed(() => graphs.value.find(graph => graph.graph_id === workflow.value))
const modesMismatch = computed(() => {
  const modes = currentDraft.value?.allowed_modes
  const supported = selectedGraph.value?.capabilities.modes
  return Array.isArray(modes) && !!supported?.length && !supported.some(mode => modes.includes(mode))
})
async function loadGraphs() {
  const current = ++graphGeneration
  graphsLoading.value = true; graphError.value = ''
  try {
    const value = await graphService.list()
    if (!Array.isArray(value)) throw new Error('目录格式无效。')
    if (current === graphGeneration) graphs.value = value
  } catch (e) { if (current === graphGeneration) { graphs.value = []; graphError.value = `加载工作流失败：${agentError(e)}` } }
  finally { if (current === graphGeneration) graphsLoading.value = false }
}
function refKey(value: unknown): string {
  if (!value || typeof value !== 'object') return ''
  const ref = value as Partial<ModelRef>
  return typeof ref.resource_id === 'string' && typeof ref.version === 'string' && typeof ref.content_hash === 'string'
    ? JSON.stringify([ref.resource_id, ref.version, ref.content_hash]) : ''
}
const modelKey = computed(() => {
  if (!selected.value?.draft) return ''
  try { return refKey(parseAgentDraft(editor.value, selected.value.draft).default_model) }
  catch { return '' }
})
let generation = 0
let operation = 0
let service = new AgentApi(api)

function discardChanges(): boolean {
  return !dirty.value || window.confirm('当前配置有未保存的修改，是否放弃？')
}
function confirmNavigation(event: MouseEvent) {
  if (!discardChanges()) event.preventDefault()
}

function showDetail(detail: AgentDetail) {
  selected.value = detail
  editor.value = detail.draft ? JSON.stringify(detail.draft, null, 2) : ''
  baseline.value = editor.value
  invalidNumbers.value = {}
  stale.value = false
}

async function loadList() {
  const current = generation
  loading.value = true
  try {
    const result = await service.list()
    if (current === generation) items.value = result
  } catch (e) { if (current === generation) error.value = agentError(e) }
  finally { if (current === generation) loading.value = false }
}

async function openAgent(id: string, force = false) {
  if (!force && !discardChanges()) return
  const current = ++generation
  selected.value = null
  editor.value = baseline.value = ''
  invalidNumbers.value = {}
  models.value = []; modelError.value = ''; modelsLoading.value = false
  skills.value = []; skillError.value = ''; skillsLoading.value = false
  skillDialogOpen.value = false; skillSearch.value = ''; pendingSkills.value = []
  stale.value = false
  error.value = notice.value = ''
  loading.value = true
  try {
    const detail = await service.get(id)
    if (current === generation) {
      showDetail(detail)
      await loadModels(id, current)
      await loadSkills(current)
    }
  } catch (e) { if (current === generation) error.value = agentError(e) }
  finally { if (current === generation) loading.value = false }
}

async function loadSkills(current = generation) {
  skillsLoading.value = true; skillError.value = ''
  try {
    const result = await skillService.list()
    if (!Array.isArray(result)) throw new Error('技能列表格式无效。')
    if (current === generation) skills.value = result
  } catch (e) {
    if (current === generation) { skills.value = []; skillError.value = `加载技能失败：${agentError(e)}` }
  } finally { if (current === generation) skillsLoading.value = false }
}

const skillRefs = computed<ModelRef[]>(() => Array.isArray(currentDraft.value?.skills) ? currentDraft.value.skills as ModelRef[] : [])
const boundSkills = computed(() => skillRefs.value.map(ref => ({ ref, skill: skills.value.find(skill =>
  skill.versions.some(version => refKey(version) === refKey(ref))) })))
const filteredSkills = computed(() => {
  const query = skillSearch.value.trim().toLowerCase()
  return skills.value.filter(skill => `${skill.name} ${skill.description}`.toLowerCase().includes(query))
})
function chosenSkill(skill: Skill): ModelRef | undefined {
  return skillRefs.value.find(ref => skill.versions.some(version => ref.resource_id === version.resource_id && ref.version === version.version && ref.content_hash === version.content_hash))
}
function openSkillDialog() {
  skillSearch.value = ''; pendingSkills.value = []; skillDialogOpen.value = true
  void nextTick(() => { if (skillDialogOpen.value) skillSearchInput.value?.focus?.() })
  void loadSkills()
}
function closeSkillDialog() {
  skillDialogOpen.value = false
  void nextTick(() => skillAddButton.value?.focus?.())
}
function handleSkillDialogKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') { event.stopPropagation(); closeSkillDialog(); return }
  if (event.key !== 'Tab' || !skillDialog.value) return
  const focusable = [...skillDialog.value.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex="-1"])')]
  if (!focusable.length) return
  const first = focusable[0], last = focusable[focusable.length - 1]
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
}
function togglePendingSkill(skill: Skill, checked: boolean) {
  if (chosenSkill(skill) || !skill.latest_version) return
  pendingSkills.value = checked ? [...pendingSkills.value, skill.skill_id] : pendingSkills.value.filter(id => id !== skill.skill_id)
}
function addPendingSkills() {
  if (!currentDraft.value) return
  const refs = [...skillRefs.value]
  for (const id of pendingSkills.value) {
    const skill = skills.value.find(item => item.skill_id === id)
    if (!skill?.latest_version || chosenSkill(skill)) continue
    const { resource_id, version, content_hash } = skill.latest_version
    refs.push({ resource_id, version, content_hash })
  }
  if (refs.length > 16) return
  editor.value = JSON.stringify({ ...currentDraft.value, skills: refs }, null, 2)
  closeSkillDialog(); pendingSkills.value = []
}
function updateSkill(skill: Skill, key: string) {
  if (!currentDraft.value) return
  const own = new Set(skill.versions.map(version => refKey(version)))
  const version = skill.versions.find(item => refKey(item) === key)
  const replacement = version && { resource_id: version.resource_id, version: version.version, content_hash: version.content_hash }
  const index = skillRefs.value.findIndex(ref => own.has(refKey(ref)))
  const refs = skillRefs.value.filter(ref => !own.has(refKey(ref)))
  if (replacement) refs.splice(index < 0 ? refs.length : index, 0, replacement)
  editor.value = JSON.stringify({ ...currentDraft.value, skills: refs }, null, 2)
}
function removeUnknownSkill(ref: ModelRef) {
  if (!currentDraft.value) return
  editor.value = JSON.stringify({ ...currentDraft.value, skills: skillRefs.value.filter(item => refKey(item) !== refKey(ref)) }, null, 2)
}

async function loadModels(id: string, current = generation) {
  modelsLoading.value = true; modelError.value = ''
  try {
    const result = await service.models(id)
    if (current === generation) models.value = result
  } catch (e) {
    if (current === generation) { models.value = []; modelError.value = `加载模型版本失败：${agentError(e)}` }
  } finally { if (current === generation) modelsLoading.value = false }
}

watch(bearerToken, () => {
  ++generation
  ++operation
  ++graphGeneration
  graphs.value = []; graphError.value = ''; graphsLoading.value = false
  service = new AgentApi(api)
  items.value = []; selected.value = null; editor.value = baseline.value = ''; name.value = ''
  invalidNumbers.value = {}
  models.value = []; modelError.value = ''; modelsLoading.value = false
  skills.value = []; skillError.value = ''; skillsLoading.value = false
  skillDialogOpen.value = false; skillSearch.value = ''; pendingSkills.value = []
  error.value = notice.value = ''; stale.value = false; loading.value = busy.value = false
  if (bearerToken.value) { void loadList(); void loadGraphs() }
}, { immediate: true })
onUnmounted(() => { ++generation; ++operation; ++graphGeneration })

async function createAgent() {
  const trimmed = name.value.trim()
  if (!trimmed || trimmed.length > 128) { error.value = 'Agent 名称须为 1 至 128 个字符。'; return }
  if (!discardChanges()) return
  const current = generation
  const activeOperation = ++operation
  error.value = notice.value = ''; busy.value = true
  try {
    const created = await service.create(trimmed)
    if (current !== generation) return
    name.value = ''
    await openAgent(created.agent_id, true)
    if (activeOperation !== operation) return
    notice.value = 'Agent 已创建。'
    await loadList()
  } catch (e) { if (current === generation) error.value = agentError(e) }
  finally { if (activeOperation === operation) busy.value = false }
}

function markError(e: unknown) {
  error.value = agentError(e)
  if (e instanceof ApiError && e.status === 409 && e.message.includes('REVISION_CONFLICT')) stale.value = true
}

function changeWorkflow(event: Event) {
  if (!selected.value?.draft) return
  try {
    const draft = parseAgentDraft(editor.value, selected.value.draft)
    draft.graph_id = (event.target as HTMLSelectElement).value
    editor.value = JSON.stringify(draft, null, 2)
    error.value = ''
  } catch (e) { error.value = agentError(e) }
}

function changeModel(event: Event) {
  if (!selected.value?.draft) return
  const model = models.value.find(item => refKey(item.active_ref) === (event.target as HTMLSelectElement).value)
  if (!model) return
  try {
    const draft = parseAgentDraft(editor.value, selected.value.draft)
    draft.default_model = { ...model.active_ref }
    editor.value = JSON.stringify(draft, null, 2)
    error.value = ''
  } catch (e) { error.value = agentError(e) }
}

function updateField(key: string, value: string) {
  if (!currentDraft.value) return
  editor.value = JSON.stringify({ ...currentDraft.value, [key]: value }, null, 2)
}

function eventValue(event: Event): string { return (event.target as HTMLInputElement).value }
function eventChecked(event: Event): boolean { return (event.target as HTMLInputElement).checked }
function textValue(key: string): string {
  const value = currentDraft.value?.[key]
  return typeof value === 'string' ? value : ''
}

function numberValue(key: string, budget = false): string | number {
  if (key in invalidNumbers.value) return invalidNumbers.value[key]
  const value = budget && currentDraft.value?.budget_limits && typeof currentDraft.value.budget_limits === 'object'
    ? (currentDraft.value.budget_limits as Draft)[key] : currentDraft.value?.[key]
  return typeof value === 'number' ? value : budget ? '' : 8
}

function updateNumber(key: string, value: string, budget = false) {
  if (!currentDraft.value) return
  const number = Number(value)
  const valid = budget && value === '' || value !== '' && /^\d+$/.test(value) && Number.isSafeInteger(number) && (budget ? number > 0 : number <= 8)
  const pending = { ...invalidNumbers.value }
  if (!valid) { pending[key] = value; invalidNumbers.value = pending; return }
  delete pending[key]
  invalidNumbers.value = pending
  const draft = currentDraft.value
  if (budget) {
    const existing = draft.budget_limits
    const limits = existing && typeof existing === 'object' && !Array.isArray(existing) ? existing : {}
    draft.budget_limits = { ...limits, [key]: value === '' ? null : number }
  } else draft[key] = number
  editor.value = JSON.stringify(draft, null, 2)
}

function updateKnowledgeAge(value: string) {
  if (!currentDraft.value) return
  const number = Number(value), pending = { ...invalidNumbers.value }
  if (value !== '' && (!/^\d+$/.test(value) || !Number.isSafeInteger(number) || number <= 0)) {
    pending.knowledge_max_age_seconds = value; invalidNumbers.value = pending; return
  }
  delete pending.knowledge_max_age_seconds
  invalidNumbers.value = pending
  editor.value = JSON.stringify({ ...currentDraft.value, knowledge_max_age_seconds: value === '' ? null : number }, null, 2)
}

async function save() {
  const detail = selected.value
  if (!detail || !detail.draft || stale.value || validationError.value) return
  let draft
  try { draft = parseAgentDraft(editor.value, detail.draft) }
  catch (e) { error.value = agentError(e); return }
  const current = generation
  error.value = notice.value = ''; busy.value = true
  try {
    const result = await service.save(detail.agent_id, detail.revision, draft)
    if (current !== generation) return
    showDetail({ ...detail, revision: result.revision, draft: result.draft })
    notice.value = '草稿已保存。'
  } catch (e) { if (current === generation) markError(e) }
  finally { if (current === generation) busy.value = false }
}

async function publish() {
  const detail = selected.value
  if (!detail || !detail.draft || dirty.value || stale.value) return
  if (!window.confirm(`确认发布 ${detail.name} 的草稿（Revision ${detail.revision}）？`)) return
  const current = generation
  error.value = notice.value = ''; busy.value = true
  try {
    const published = await service.publish(detail.agent_id, detail.revision)
    if (current !== generation) return
    stale.value = true
    notice.value = `已发布版本 ${published.version}。`
    try {
      const updated = await service.get(detail.agent_id)
      if (current === generation) showDetail(updated)
    } catch (e) { if (current === generation) error.value = `发布成功，但刷新失败：${agentError(e)}` }
  } catch (e) { if (current === generation) markError(e) }
  finally { if (current === generation) busy.value = false }
}
</script>

<template>
  <div class="page-heading"><div><div class="eyebrow">AGENT <span></span> 管理</div><h1>Agent 管理</h1></div></div>
  <section class="panel qa-panel">
    <div class="qa-form">
      <p v-if="!bearerToken" class="form-note">请先提供 Bearer Token。</p>
      <p v-if="error" class="form-error" role="alert">{{ error }}</p>
      <p v-if="notice" class="form-note" role="status">{{ notice }}</p>
      <form class="create-form" @submit.prevent="createAgent">
        <label>新建 Agent<input v-model="name" maxlength="128" placeholder="名称" :disabled="!bearerToken || busy || loading" /></label>
        <div class="form-actions"><button class="primary-button" type="submit" :disabled="!bearerToken || busy || loading || !name.trim()">创建</button></div>
      </form>
      <div class="list-heading"><h2>Agent 列表</h2><div class="form-actions"><button type="button" :disabled="!bearerToken || busy || loading" @click="loadList">刷新列表</button></div></div>
      <p v-if="loading" class="form-note">加载中…</p>
      <p v-else-if="bearerToken && !items.length && !error" class="form-note">没有可访问的 Agent。</p>
      <ul v-if="items.length" class="agent-list">
        <li v-for="item in items" :key="item.agent_id"><button type="button" :disabled="busy || loading" :aria-current="selected?.agent_id === item.agent_id ? 'true' : undefined" @click="openAgent(item.agent_id)"><strong>{{ item.name }}</strong><small>{{ item.agent_id }}</small></button></li>
      </ul>
      <template v-if="selected">
        <fieldset><legend>{{ selected.name }}</legend>
          <p class="form-note">ID: {{ selected.agent_id }} · Revision: {{ selected.revision }} · {{ selected.active_profile_id ? '已发布' : '未发布' }}</p>
          <p v-if="!selected.draft" class="form-note">此 Agent 没有可编辑的草稿。</p>
          <template v-else>
            <label>工作流<select :value="workflow" :disabled="busy || graphsLoading || !!graphError || !graphs.length" @change="changeWorkflow">
              <option v-if="!workflow" value="" disabled>请先修正配置 JSON</option>
              <option v-if="workflow && !selectedGraph" :value="workflow">当前工作流不可用</option>
              <option v-for="graph in graphs" :key="graph.graph_id" :value="graph.graph_id">{{ graph.title }} · {{ graph.summary }}</option>
            </select></label>
            <p v-if="graphsLoading" class="form-note">正在加载工作流…</p>
            <p v-else-if="graphError" class="form-error" role="alert">{{ graphError }}</p>
            <button v-if="graphError" type="button" @click="loadGraphs">重试加载工作流</button>
            <div v-if="selectedGraph" class="form-note graph-hint">
              <p>{{ selectedGraph.summary }}</p>
              <p v-if="selectedGraph.status === 'experimental'">实验工作流：Worker 需要显式启用对应能力。</p>
              <p v-if="selectedGraph.capabilities.needs_knowledge_tool">需要知识工具与查询范围。</p>
              <p v-if="selectedGraph.capabilities.needs_workflow">运行时需要已发布的工作流。</p>
              <p v-if="selectedGraph.capabilities.entrypoints.includes('synthetic_alert')">仅合成告警入口；当前适配器仅供本地集成测试。</p>
              <p v-if="workflow === 'knowledge.citation_review'">真实知识源和范围隔离尚未验收，完成核对后才能在真实环境启用。</p>
              <p v-if="modesMismatch">当前允许的运行模式与所选工作流不匹配。请在配置草稿 JSON 的 allowed_modes 中启用所需模式：{{ selectedGraph.capabilities.modes.join('、') }}。</p>
              <a :href="`/app/graphs/${encodeURIComponent(selectedGraph.graph_id)}`" @click="confirmNavigation">查看结构</a>
            </div>
            <label v-if="workflow === 'knowledge.citation_review'">知识来源有效期（秒）<input type="number" min="1" step="1" :value="invalidNumbers.knowledge_max_age_seconds ?? currentDraft?.knowledge_max_age_seconds ?? ''" :disabled="busy || !currentDraft" @input="updateKnowledgeAge(eventValue($event))" /></label>
            <p v-if="workflow === 'knowledge.citation_review'" class="form-note">未设置有效期或来源时间不可验证时，知识问答不会标记为完成。</p>
            <label>默认模型<select :value="modelKey" :disabled="busy || modelsLoading || !!modelError || !models.length" @change="changeModel">
              <option v-if="!modelKey || !models.some(model => refKey(model.active_ref) === modelKey)" :value="modelKey">当前模型{{ modelKey ? ` (${JSON.parse(modelKey)[0]} · ${JSON.parse(modelKey)[1]})` : '' }}</option>
              <option v-for="model in models" :key="refKey(model.active_ref)" :value="refKey(model.active_ref)">{{ model.name }} · 版本 {{ model.active_ref.version }}</option>
            </select></label>
            <p v-if="modelsLoading" class="form-note">加载模型版本中…</p>
            <p v-else-if="modelError" class="form-error" role="alert">{{ modelError }}</p>
            <p v-else-if="!models.length" class="form-note">没有可选择的已发布模型版本，当前 ref 保持不变。</p>
            <div v-if="modelError" class="form-actions"><button type="button" :disabled="busy || modelsLoading" @click="loadModels(selected.agent_id)">重试加载模型</button></div>
            <p class="form-note">模型参数在 <a href="/app/models" @click="confirmNavigation">模型管理</a> 中配置。</p>
            <div class="settings-group">
              <div class="skill-heading"><h2>技能配置 <span class="skill-count">{{ skillRefs.length }}</span></h2><div class="skill-actions"><a href="/app/skills" @click="confirmNavigation">管理 Skills</a><button ref="skillAddButton" type="button" class="primary-button add-skill" :disabled="busy || !currentDraft" @click="openSkillDialog"><Plus class="skill-icon" />添加 Skill</button></div></div>
              <p class="form-note">保存并发布 Agent 后，新任务使用所选技能版本。</p>
              <p v-if="skillsLoading" class="form-note">加载技能中…</p>
              <p v-else-if="skillError" class="form-error" role="alert">{{ skillError }}</p>
              <p v-else-if="!skillRefs.length" class="form-note skill-empty">尚未添加 Skill。</p>
              <div v-for="binding in boundSkills" :key="refKey(binding.ref)" class="skill-binding">
                <template v-if="binding.skill"><label class="binding-info">{{ binding.skill.name }}<small>{{ binding.skill.description }}</small><select class="binding-version" :value="refKey(binding.ref)" :disabled="busy" @change="updateSkill(binding.skill, eventValue($event))"><option v-for="version in binding.skill.versions" :key="version.resource_id" :value="refKey(version)">版本 {{ version.version }}</option></select></label></template>
                <div v-else class="binding-info"><strong>无法解析的技能</strong><small>{{ binding.ref.resource_id }} · 版本 {{ binding.ref.version }}</small></div>
                <button type="button" class="icon-button remove-skill" :disabled="busy" :aria-label="`移除 ${binding.skill?.name ?? binding.ref.resource_id}`" :title="`移除 ${binding.skill?.name ?? binding.ref.resource_id}`" @click="removeUnknownSkill(binding.ref)"><Delete class="skill-icon" /></button>
              </div>
              <div v-if="skillDialogOpen" class="skill-overlay" @click.self="closeSkillDialog" @keydown="handleSkillDialogKeydown">
                <section ref="skillDialog" class="skill-dialog" role="dialog" aria-modal="true" aria-labelledby="skill-dialog-title">
                  <header class="dialog-header"><h2 id="skill-dialog-title">添加 Skill</h2><button type="button" class="icon-button" aria-label="关闭" title="关闭" @click="closeSkillDialog"><Close class="skill-icon" /></button></header>
                  <div class="dialog-body"><label>搜索 Skill<input ref="skillSearchInput" :value="skillSearch" placeholder="搜索技能名称或描述" @input="skillSearch = eventValue($event)" /></label>
                    <p v-if="skillsLoading" class="form-note">加载技能中…</p>
                    <div v-else-if="skillError"><p class="form-error" role="alert">{{ skillError }}</p><button type="button" @click="loadSkills()">重试</button></div>
                    <p v-else-if="!filteredSkills.length" class="form-note skill-empty">没有匹配的 Skill。</p>
                    <div v-else class="skill-options"><label v-for="skill in filteredSkills" :key="skill.skill_id" class="skill-option"><input type="checkbox" :checked="pendingSkills.includes(skill.skill_id)" :disabled="!!chosenSkill(skill) || !skill.latest_version || (!pendingSkills.includes(skill.skill_id) && skillRefs.length + pendingSkills.length >= 16)" @change="togglePendingSkill(skill, eventChecked($event))" /><span class="option-details"><strong>{{ skill.name }}</strong><small>{{ skill.description }}</small></span><span class="option-state">{{ chosenSkill(skill) ? '已添加' : skill.latest_version ? `版本 ${skill.latest_version.version}` : '未发布' }}</span></label></div>
                  </div>
                  <footer class="dialog-footer"><span>已选择 {{ pendingSkills.length }} 个</span><div class="skill-actions"><button type="button" @click="closeSkillDialog">取消</button><button type="button" class="primary-button" :disabled="!pendingSkills.length || skillsLoading || !!skillError || skillRefs.length + pendingSkills.length > 16" @click="addPendingSkills">添加所选</button></div></footer>
                </section>
              </div>
            </div>
            <div class="settings-group">
              <h2>Agent 描述与提示词</h2>
              <label>描述<textarea :value="textValue('description')" maxlength="1000" rows="3" :disabled="busy || !currentDraft" @input="updateField('description', eventValue($event))" /></label>
              <label>系统提示词<textarea :value="textValue('system_prompt')" maxlength="12000" rows="6" :disabled="busy || !currentDraft" @input="updateField('system_prompt', eventValue($event))" /></label>
              <p class="form-note">系统提示词、回复语言和回复格式作为提示指令提供给模型；JSON 格式不保证输出一定是有效 JSON。</p>
              <div class="settings-grid">
                <label>回复语言<select :value="currentDraft?.response_language ?? 'auto'" :disabled="busy || !currentDraft" @change="updateField('response_language', eventValue($event))"><option value="auto">自动</option><option value="zh">中文</option><option value="en">English</option></select></label>
                <label>回复格式<select :value="currentDraft?.response_format ?? 'default'" :disabled="busy || !currentDraft" @change="updateField('response_format', eventValue($event))"><option value="default">默认</option><option value="markdown">Markdown</option><option value="json">JSON</option></select></label>
              </div>
            </div>
            <div class="settings-group">
              <h2>上下文管理</h2>
              <div class="settings-grid"><label>对话历史轮数<input type="number" min="0" max="8" step="1" :value="numberValue('conversation_history_turns')" :disabled="busy || !currentDraft || workflow !== 'conversation.multi_turn'" @input="updateNumber('conversation_history_turns', eventValue($event))" /></label></div>
              <p class="form-note">模型最多参考最近的历史对话轮数；每轮包含一个问题和回答。设为 0 时不参考历史，不会删除会话记录。仅适用于多轮对话。</p>
            </div>
            <div class="settings-group">
              <h2>预算上限</h2>
              <div class="settings-grid"><label v-for="limit in budgetFields" :key="limit.key">{{ limit.label }}<input type="number" min="1" step="1" :value="numberValue(limit.key, true)" :disabled="busy || !currentDraft" @input="updateNumber(limit.key, eventValue($event), true)" /></label></div>
              <p class="form-note">留空表示未设置上限。工具调用上限不启用工具。</p>
            </div>
            <label>配置草稿 JSON<textarea v-model="editor" rows="18" spellcheck="false" :disabled="busy" @input="invalidNumbers = {}" /></label>
            <p class="form-note">省略的已有字段会保留；使用空数组或可空字段的 null 明确清除。发布前请先保存草稿。</p>
            <p v-if="validationError && dirty" class="form-error" role="alert">{{ validationError }}</p>
            <div class="form-actions">
              <button class="primary-button" type="button" :disabled="busy || stale || !dirty || !!validationError" @click="save">保存草稿</button>
              <button type="button" :disabled="busy || stale || dirty" @click="publish">发布草稿</button>
              <button type="button" :disabled="busy || loading" @click="openAgent(selected.agent_id)">重新加载配置</button>
            </div>
          </template>
        </fieldset>
      </template>
    </div>
  </section>
</template>

<style scoped>
.create-form{display:flex;align-items:end;gap:12px;flex-wrap:wrap}.create-form label{flex:1;min-width:180px}.list-heading{display:flex;align-items:center;justify-content:space-between;gap:12px}.list-heading h2{font-size:16px;margin:0}.agent-list{list-style:none;margin:0;padding:0;border-top:1px solid #e2e9ea}.agent-list li{border-bottom:1px solid #e2e9ea}.agent-list button{width:100%;border:0;background:transparent;text-align:left;padding:12px;display:flex;gap:8px;flex-wrap:wrap;align-items:baseline;color:#344852;cursor:pointer}.agent-list button:hover,.agent-list button[aria-current=true]{background:#eff8f6}.agent-list button:disabled{cursor:not-allowed;opacity:.6}.agent-list small{color:#61747a;overflow-wrap:anywhere}.qa-form textarea{font-family:monospace;line-height:1.5}.qa-form fieldset .form-actions{margin-top:12px}
.settings-group{display:grid;gap:12px;margin:20px 0}.settings-group h2{font-size:15px;margin:0}.settings-group .form-note{margin:0}.settings-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}.settings-group textarea{font-family:inherit}
.skill-heading,.skill-actions{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.skill-heading{justify-content:space-between}.skill-count{color:#61747a;background:#edf2f3;border-radius:4px;padding:2px 7px;font-size:12px}.skill-icon{width:16px;height:16px;flex:none}.add-skill{display:inline-flex;align-items:center;gap:5px}.skill-binding{display:flex;align-items:center;gap:12px;padding:12px 0;border-bottom:1px solid #e2e9ea}.binding-info{flex:1;min-width:0;display:grid!important;grid-template-columns:minmax(0,1fr) 132px;gap:4px 12px;align-items:center;overflow-wrap:anywhere}.binding-info small{grid-column:1;color:#61747a;font-weight:normal}.binding-info .binding-version{grid-column:2;grid-row:1 / 3;margin:0}.icon-button{display:inline-flex;align-items:center;justify-content:center;width:34px;height:34px;padding:0}.skill-empty{padding:12px 0}.skill-overlay{position:fixed;inset:0;z-index:1000;background:rgba(20,31,39,.45);display:flex;align-items:center;justify-content:center;padding:16px}.skill-dialog{width:min(620px,100%);max-height:min(720px,90vh);background:#fff;border-radius:6px;display:flex;flex-direction:column;box-shadow:0 16px 48px #1d2d3530}.dialog-header,.dialog-footer{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:16px 20px}.dialog-header{border-bottom:1px solid #e2e9ea}.dialog-body{padding:16px 20px;overflow:auto}.dialog-footer{border-top:1px solid #e2e9ea;color:#61747a}.skill-option{display:flex!important;align-items:flex-start;gap:12px;padding:14px 0;border-bottom:1px solid #e2e9ea;cursor:pointer}.skill-option input{width:16px!important;height:16px;margin:3px 0 0!important;flex:none;accent-color:#177a6e}.option-details{display:grid;gap:3px;flex:1;min-width:0;overflow-wrap:anywhere}.option-details small{font-weight:normal;color:#61747a}.option-state{font-size:12px;color:#61747a;white-space:nowrap}@media(max-width:560px){.binding-info{grid-template-columns:1fr}.binding-info .binding-version{grid-column:1;grid-row:auto}.dialog-footer{flex-wrap:wrap}}
.add-skill,.dialog-footer button,.dialog-body button,.icon-button{border:1px solid #cbd6db;border-radius:4px;background:#fff;color:#344852;font:inherit;cursor:pointer}.add-skill,.dialog-footer button,.dialog-body button{min-height:38px;padding:0 14px}.add-skill,.dialog-footer .primary-button{background:#167b6e;border-color:#167b6e;color:#fff;font-weight:600}.icon-button:hover,.dialog-footer button:not(.primary-button):hover{background:#eff8f6}.add-skill:disabled,.dialog-footer button:disabled,.icon-button:disabled{opacity:.55;cursor:not-allowed}
</style>
