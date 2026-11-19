<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { Refresh, Plus } from '@element-plus/icons-vue'
import { api, bearerToken } from '../session'
import { copyModelDraft, createModelsApi, type ModelDraft, type ModelProfile, type ProbeJob } from '../models-api'

const defaultDraft = (): ModelDraft => ({
  name: '', provider: 'deepseek', adapter: 'deepseek', protocol: 'openai_chat_completions',
  base_url: 'https://api.deepseek.com', model_id: 'deepseek-flash', secret_ref: 'env://DEEPSEEK_API_KEY',
  temperature: 0, request_timeout_seconds: 60, max_output_tokens: 4096, max_context_tokens: 64000,
  streaming: true, sdk_max_retries: 0, invocation_max_attempts: 3,
  structured_output_strategy: 'json_mode', provider_options: { thinking: { type: 'disabled' } },
})
const profiles = ref<ModelProfile[]>([])
const selected = ref<ModelProfile | null>(null), draft = ref<ModelDraft | null>(null), creating = ref(false)
const loading = ref(false), busy = ref(false), error = ref(''), notice = ref(''), job = ref<ProbeJob | null>(null)
const apiKey = ref('')
const dirty = computed(() => !!selected.value && !!draft.value && JSON.stringify(draft.value) !== JSON.stringify(selected.value.draft))
type ProbeReport = { configuration?: string; requested_model?: string; response_model?: string | null; capabilities?: Record<string, boolean> }
const probeReport = computed<ProbeReport | null>(() => {
  if (!job.value?.result_ref || job.value.status !== 'succeeded') return null
  try { return JSON.parse(job.value.result_ref) as ProbeReport } catch { return null }
})
const capabilityLabels: Record<string, string> = { text: '基础对话', tool: '工具调用', stream: '流式输出', json: 'JSON 输出', usage: '用量统计', client_stream_close: '流式中断' }
let models = createModelsApi(api), generation = 0, timer: ReturnType<typeof setTimeout> | undefined

function resetEditor() {
  selected.value = null; draft.value = null; creating.value = false; job.value = null
  apiKey.value = ''
  if (timer) clearTimeout(timer)
}
function showError(e: unknown) { error.value = e instanceof Error ? e.message : '请求失败，请重试。' }
async function loadProfiles() {
  models = createModelsApi(api); profiles.value = []; resetEditor()
  const current = ++generation
  error.value = ''; notice.value = ''; loading.value = false; busy.value = false
  if (!bearerToken.value) return
  loading.value = true
  try {
    const items = await models.list()
    if (current === generation) profiles.value = items
  } catch (e) { if (current === generation) showError(e) }
  finally { if (current === generation) loading.value = false }
}
function refreshProfiles() {
  if ((dirty.value || creating.value) && !window.confirm('当前配置有未保存的修改，是否放弃？')) return
  void loadProfiles()
}
watch(bearerToken, () => { void loadProfiles() }, { immediate: true })
onUnmounted(() => { generation++; if (timer) clearTimeout(timer) })

function choose(profile: ModelProfile, force = false) {
  if (!force && (dirty.value || creating.value) && !window.confirm('当前配置有未保存的修改，是否放弃？')) return
  resetEditor(); selected.value = profile; draft.value = copyModelDraft(profile.draft); error.value = ''; notice.value = ''
}
function create() {
  if ((dirty.value || creating.value) && !window.confirm('当前配置有未保存的修改，是否放弃？')) return
  resetEditor(); creating.value = true; draft.value = defaultDraft(); error.value = ''; notice.value = ''
}
async function save() {
  if (!draft.value || busy.value) return
  const current = generation, value = copyModelDraft(draft.value)
  error.value = ''; notice.value = ''; busy.value = true
  try {
    const result = creating.value ? await models.create(value) : await models.patch(selected.value!.name, value, selected.value!.revision)
    if (current !== generation) return
    profiles.value = [...profiles.value.filter(item => item.profile_id !== result.profile_id), result]
    choose(result, true); notice.value = '草稿已保存。'
  } catch (e) { if (current === generation) showError(e) }
  finally { if (current === generation) busy.value = false }
}
async function publish() {
  if (!selected.value || dirty.value || busy.value) return
  const current = generation, model = selected.value
  error.value = ''; notice.value = ''; busy.value = true
  try {
    await models.publish(model.name, model.revision)
    if (current !== generation) return
    const result = await models.get(model.name)
    if (current !== generation) return
    profiles.value = profiles.value.map(item => item.profile_id === result.profile_id ? result : item)
    choose(result, true); notice.value = `已发布版本 ${result.active_version}。`
  } catch (e) { if (current === generation) showError(e) }
  finally { if (current === generation) busy.value = false }
}
async function saveCredential() {
  if (!selected.value || !apiKey.value || dirty.value || busy.value) return
  const current = generation, model = selected.value, value = apiKey.value
  apiKey.value = ''; error.value = ''; notice.value = ''; busy.value = true
  try {
    const result = await models.setCredential(model.name, value, model.revision)
    if (current !== generation) return
    profiles.value = profiles.value.map(item => item.profile_id === result.profile_id ? result : item)
    choose(result, true); notice.value = '密钥已保存至草稿，请发布模型版本。'
  } catch (e) {
    if (current === generation) {
      if (e instanceof Error && e.message.includes('MODEL_CREDENTIAL_STORE_UNAVAILABLE')) error.value = '密钥存储尚未启用，请先配置主密钥。'
      else if (e instanceof Error && e.message.includes('MODEL_CREDENTIAL_UNAVAILABLE')) error.value = '密钥保存失败，请检查服务地址和密钥存储权限。'
      else if (e instanceof Error && e.message.includes('MODEL_PROFILE_UNAVAILABLE')) error.value = '服务地址尚未获得部署授权。'
      else showError(e)
    }
  }
  finally { if (current === generation) busy.value = false }
}
async function pollJob(id: string, current: number, agentId: string) {
  if (timer) clearTimeout(timer)
  try {
    const result = await models.job(agentId, id)
    if (current !== generation || job.value?.job_id !== id) return
    job.value = result
    if (['queued', 'running'].includes(result.status)) timer = setTimeout(() => { void pollJob(id, current, agentId) }, 2000)
  } catch (e) { if (current === generation && job.value?.job_id === id) showError(e) }
}
async function probe() {
  if (!selected.value || busy.value) return
  const current = generation
  error.value = ''; notice.value = ''; busy.value = true
  try {
    const result = await models.probe(selected.value.name)
    if (current !== generation) return
    job.value = result
    if (['queued', 'running'].includes(result.status)) timer = setTimeout(() => { void pollJob(result.job_id, current, result.agent_id) }, 2000)
  } catch (e) {
    if (current === generation) {
      if (e instanceof Error && e.message.includes('MODEL_PROFILE_UNAVAILABLE'))
        error.value = '验证需要至少一个已启用且获得该模型部署授权的 Agent。'
      else showError(e)
    }
  }
  finally { if (current === generation) busy.value = false }
}
</script>

<template>
  <section class="admin-page models-admin">
  <header class="admin-page-header"><div><p class="workflow-eyebrow">MODEL LIBRARY</p><h1>模型配置</h1><p>统一维护团队模型，发布后可在 Agent 管理中绑定。</p></div><span class="admin-page-count">{{ profiles.length }} 个模型</span></header>
  <div class="models-toolbar panel">
    <button type="button" title="刷新模型" aria-label="刷新模型" :disabled="loading || busy || !bearerToken" @click="refreshProfiles"><Refresh /></button>
    <button type="button" :disabled="loading || busy || !bearerToken" @click="create"><Plus /> 新建模型</button>
  </div>
  <p v-if="!bearerToken" class="form-note">请先提供 Bearer Token。</p>
  <p v-if="error" class="form-error" role="alert">{{ error }}</p>
  <p v-if="notice" role="status">{{ notice }}</p>
  <div v-if="bearerToken" class="models-layout admin-workspace">
    <section aria-label="模型列表" class="models-list admin-rail panel">
      <h2>模型列表 <span class="admin-list-count">{{ profiles.length }}</span></h2>
      <p v-if="loading">加载中...</p>
      <p v-else-if="!profiles.length">暂无模型配置。</p>
      <button v-for="item in profiles" :key="item.profile_id" type="button" class="model-row" :class="{ active: selected?.profile_id === item.profile_id }" :disabled="busy" @click="choose(item)">
        <strong>{{ item.name }}</strong><span class="admin-status" :class="item.active_version === null ? 'draft' : 'published'">{{ item.active_version === null ? '未发布' : `版本 ${item.active_version}` }}</span><small>修订 {{ item.revision }}</small>
      </button>
    </section>
    <section v-if="draft" class="models-editor admin-detail panel">
      <div class="admin-editor-heading"><div><p class="workflow-eyebrow">{{ creating ? '新建配置' : '模型详情' }}</p><h2>{{ creating ? '新建模型' : selected?.name }}</h2></div><span v-if="selected" class="admin-status" :class="selected.active_version === null ? 'draft' : 'published'">{{ selected.active_version === null ? '未发布' : `版本 ${selected.active_version}` }}</span></div>
      <p v-if="selected" class="admin-detail-meta">修订 {{ selected.revision }}<span v-if="dirty"> · 有未保存的修改</span></p>
      <form @submit.prevent="save">
        <div class="model-fields">
          <label>名称<input v-model.trim="draft.name" required :disabled="!creating || busy" /></label>
          <label>模型 ID<input v-model.trim="draft.model_id" required :disabled="busy" /></label>
          <label class="wide">服务地址<input v-model.trim="draft.base_url" type="url" required :disabled="busy" /></label>
          <label class="wide">密钥引用<input v-model.trim="draft.secret_ref" required placeholder="env://MODEL_API_KEY" :disabled="busy" /></label>
          <div class="wide credential-field">
            <label>API Key<input v-model="apiKey" type="password" autocomplete="new-password" placeholder="输入新的 API Key" :disabled="busy || creating" /></label>
            <button type="button" :disabled="busy || creating || dirty || !apiKey" @click="saveCredential">保存密钥</button>
          </div>
          <label>Temperature<input v-model.number="draft.temperature" type="number" step="0.1" required :disabled="busy" /></label>
          <label>请求超时（秒）<input v-model.number="draft.request_timeout_seconds" type="number" min="1" step="1" required :disabled="busy" /></label>
          <label>最大输出 tokens<input v-model.number="draft.max_output_tokens" type="number" min="1" step="1" required :disabled="busy" /></label>
          <label>最大上下文 tokens<input v-model.number="draft.max_context_tokens" type="number" min="1" step="1" required :disabled="busy" /></label>
          <label>最大调用次数<input v-model.number="draft.invocation_max_attempts" type="number" min="1" max="3" step="1" required :disabled="busy" /></label>
          <label class="check"><input v-model="draft.streaming" type="checkbox" :disabled="busy" /> 流式输出</label>
        </div>
        <div class="model-actions"><button type="submit" class="primary-button" :disabled="busy || (!creating && !dirty)">保存草稿</button><button v-if="selected" type="button" :disabled="busy || dirty" @click="publish">发布</button></div>
      </form>
      <section v-if="selected" class="probe-status" aria-label="连接验证" role="status">
        <h3>连接验证</h3><p>向已发布版本发送实际请求，检查模型访问和各项能力。</p>
        <button type="button" :disabled="busy || selected.active_version === null" @click="probe">开始验证</button>
        <p v-if="job && ['queued', 'running'].includes(job.status)">{{ job.status === 'queued' ? '等待验证任务执行…' : '正在验证模型连接…' }} <button type="button" @click="pollJob(job.job_id, generation, job.agent_id)">刷新状态</button></p>
        <template v-else-if="job">
          <strong v-if="probeReport?.capabilities?.text">模型可访问</strong>
          <strong v-else>模型不可访问</strong>
          <p v-if="probeReport?.configuration && probeReport.configuration !== 'valid'">配置状态：{{ probeReport.configuration }}</p>
          <p v-else-if="job.status !== 'succeeded'">验证任务失败：{{ job.result_ref || job.status }}</p>
          <p v-if="probeReport?.response_model">响应模型：{{ probeReport.response_model }}</p>
          <ul v-if="probeReport?.capabilities"><li v-for="(passed, key) in probeReport.capabilities" :key="key">{{ capabilityLabels[key] || key }}：{{ passed ? '通过' : '未通过' }}</li></ul>
        </template>
      </section>
    </section>
    <div v-else class="admin-detail panel admin-empty"><span class="admin-empty-mark">M</span><h2>选择一个模型</h2><p>在左侧打开模型，查看配置或发布新版本。</p></div>
  </div>
  </section>
</template>

<style scoped>
.models-toolbar { display:flex; align-items:end; gap:10px; flex-wrap:wrap; margin-bottom:16px; padding:15px 18px; }
.models-toolbar label, .model-fields label { display:flex; flex-direction:column; gap:6px; font-size:13px; font-weight:600; }
.models-toolbar label { min-width: min(260px, 100%); }
.models-toolbar button { display:inline-flex; align-items:center; gap:6px; min-height:39px; padding:0 12px; border:1px solid #d5deeb; border-radius:7px; background:#fff; color:#425672; cursor:pointer; }
.models-toolbar button:last-child { background:#4b6fd6; border-color:#4b6fd6; color:#fff; font-weight:650; }
.models-toolbar button:disabled { opacity:.55; cursor:not-allowed; }
.models-toolbar svg { width:16px; height:16px; }
.models-layout { margin-top:16px; }
.models-layout h2 { font-size:16px; margin:0 0 16px; }
.models-list h2 span { color:#69777e; font-weight:400; }
.model-row { display:grid; width:100%; text-align:left; justify-items:start; gap:7px; margin-top:5px; padding:12px; border:1px solid transparent; border-radius:8px; background:transparent; cursor:pointer; }
.model-row:hover,.model-row.active { background:#edf2ff; border-color:#dce5fc; }
.model-row strong { color:#354963; font-size:12px; }
.model-row small { color:#5b6d85; font-size:10px; }
.models-editor { min-width:0; max-width:none; }
.admin-editor-heading { display:flex; align-items:center; justify-content:space-between; gap:10px; }
.admin-editor-heading h2 { margin:0; font-size:22px; }
.models-editor > p, .empty-editor { color:#59666a; font-size:13px; }
.model-fields { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:16px; margin:20px 0; }
.model-fields .wide { grid-column:1 / -1; }
.credential-field { display:flex; align-items:end; gap:10px; }
.credential-field label { flex:1; min-width:0; }
.model-fields input, .models-toolbar select { min-width:0; width:100%; padding:10px; border:1px solid #d5deeb; border-radius:7px; background:#fff; }
.model-fields .check { flex-direction:row; align-items:center; }
.model-fields .check input { width:auto; }
.model-actions { display:flex; flex-wrap:wrap; gap:8px; padding-top:17px; border-top:1px solid #edf1f6; }
.probe-status { display:grid; gap:8px; margin-top:24px; padding-top:16px; border-top:1px solid #dce2e5; overflow-wrap:anywhere; font-size:13px; }
.probe-status h3,.probe-status p { margin:0; }
.probe-status ul { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; margin:4px 0; padding-left:20px; }
.probe-status button { width:max-content; padding:8px 12px; border:1px solid #c8d5ee; border-radius:7px; background:#f2f6ff; color:#334f94; cursor:pointer; }
.probe-status pre { white-space:pre-wrap; overflow-wrap:anywhere; }
.probe-status button { justify-self:start; }
@media (max-width:700px) { .models-layout { grid-template-columns:1fr; } .model-fields { grid-template-columns:1fr; } .model-fields .wide { grid-column:auto; } }
</style>
