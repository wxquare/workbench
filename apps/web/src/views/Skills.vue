<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { Plus, Refresh } from '@element-plus/icons-vue'
import { api, bearerToken } from '../session'
import { agentError } from '../agents-api'
import { SkillApi, type Skill, type SkillDraft } from '../skills-api'

const service = new SkillApi(api)
const skills = ref<Skill[]>([]), selected = ref<Skill | null>(null)
const search = ref(''), name = ref(''), description = ref(''), content = ref('')
const baseline = ref(''), error = ref(''), notice = ref(''), historical = ref('')
const busy = ref(false), loading = ref(false), creating = ref(false)
let generation = 0
const filtered = computed(() => skills.value.filter(item => `${item.name} ${item.description}`.toLowerCase().includes(search.value.toLowerCase())))
const draft = computed<SkillDraft>(() => ({ description: description.value, content: content.value }))
const snapshot = computed(() => JSON.stringify({ name: creating.value ? name.value : '', ...draft.value }))
const dirty = computed(() => snapshot.value !== baseline.value)
function canLeave() { return !dirty.value || window.confirm('当前草稿有未保存的修改，是否放弃？') }
onBeforeRouteLeave(() => canLeave())
function beforeUnload(event: BeforeUnloadEvent) { if (dirty.value) { event.preventDefault(); event.returnValue = '' } }
if (typeof window !== 'undefined' && window.addEventListener) window.addEventListener('beforeunload', beforeUnload)
function show(skill: Skill) {
  selected.value = skill; creating.value = false; historical.value = ''
  name.value = skill.name; description.value = skill.draft?.description ?? ''; content.value = skill.draft?.content ?? ''
  baseline.value = snapshot.value
}
async function load() {
  const current = generation; loading.value = true
  try { const result = await service.list(); if (current === generation) skills.value = result }
  catch (e) { if (current === generation) error.value = agentError(e) }
  finally { if (current === generation) loading.value = false }
}
async function open(id: string) {
  if (!canLeave()) return
  const current = ++generation; error.value = notice.value = ''; loading.value = true
  try { const result = await service.get(id); if (current === generation) show(result) }
  catch (e) { if (current === generation) error.value = agentError(e) }
  finally { if (current === generation) loading.value = false }
}
function startCreate() {
  if (!canLeave()) return
  ++generation; selected.value = null; creating.value = true; name.value = description.value = content.value = historical.value = ''
  baseline.value = snapshot.value; error.value = notice.value = ''
}
async function save() {
  if (!description.value.trim() || !content.value.trim()) return
  const current = generation; busy.value = true; error.value = notice.value = ''
  try {
    const result = creating.value ? await service.create(name.value.trim(), draft.value) : await service.save(selected.value!.skill_id, selected.value!.revision, draft.value)
    if (current !== generation) return
    show(result); notice.value = '草稿已保存。'; await load()
  } catch (e) { if (current === generation) error.value = agentError(e) }
  finally { if (current === generation) busy.value = false }
}
async function publish() {
  if (!selected.value || dirty.value || !window.confirm(`发布 ${selected.value.name} 的当前草稿？`)) return
  const current = generation, id = selected.value.skill_id; busy.value = true; error.value = notice.value = ''
  try {
    const result = await service.publish(id, selected.value.revision)
    if (current !== generation) return
    notice.value = `已发布版本 ${result.version}。`
    try {
      const updated = await service.get(id); if (current === generation) { show(updated); await load() }
    } catch (e) { if (current === generation) error.value = `发布成功，但刷新失败：${agentError(e)}` }
  } catch (e) { if (current === generation) error.value = agentError(e) }
  finally { if (current === generation) busy.value = false }
}
async function viewVersion(version: string) {
  if (!selected.value) return
  const current = generation
  try { const value = await service.version(selected.value.skill_id, version); if (current === generation) historical.value = value.content }
  catch (e) { if (current === generation) error.value = agentError(e) }
}
watch(bearerToken, () => {
  ++generation; skills.value = []; selected.value = null; creating.value = false
  name.value = description.value = content.value = historical.value = ''; baseline.value = snapshot.value
  error.value = notice.value = ''; busy.value = loading.value = false
  if (bearerToken.value) void load()
}, { immediate: true })
onUnmounted(() => { ++generation; if (typeof window !== 'undefined' && window.removeEventListener) window.removeEventListener('beforeunload', beforeUnload) })
</script>

<template>
  <div class="page-heading"><div><div class="eyebrow">AGENT <span></span> 管理</div><h1>Skills</h1></div></div>
  <section class="panel qa-panel"><div class="qa-form">
    <p v-if="!bearerToken" class="form-note">请先提供 Bearer Token。</p>
    <p v-if="error" class="form-error" role="alert">{{ error }}</p>
    <p v-if="notice" class="form-note" role="status">{{ notice }}</p>
    <div class="library-toolbar"><label>搜索 Skill<input v-model="search" placeholder="名称或描述" /></label><button class="primary-button" :disabled="!bearerToken || busy" @click="startCreate"><el-icon><Plus /></el-icon> 新建 Skill</button><button :disabled="!bearerToken || busy" title="刷新" aria-label="刷新" @click="load"><el-icon><Refresh /></el-icon></button></div>
    <p v-if="loading" class="form-note">加载中…</p><p v-else-if="bearerToken && !filtered.length" class="form-note">没有匹配的 Skill。</p>
    <ul class="skill-list"><li v-for="item in filtered" :key="item.skill_id"><button :disabled="busy" :aria-current="selected?.skill_id === item.skill_id ? 'true' : undefined" @click="open(item.skill_id)"><strong>{{ item.name }}</strong><span>{{ item.description }}</span><small>{{ item.latest_version ? `版本 ${item.latest_version.version}` : '未发布' }}</small></button></li></ul>
    <fieldset v-if="selected || creating" :disabled="loading || busy"><legend>{{ creating ? '新建 Skill' : selected?.name }}</legend>
      <label>技能标识<input v-model="name" :disabled="!creating || busy" maxlength="128" placeholder="例如 value-investing" /></label>
      <label>描述<textarea v-model="description" :disabled="busy || (!!selected && !selected.draft)" maxlength="1000" rows="3" /></label>
      <label>技能指令<textarea v-model="content" :disabled="busy || (!!selected && !selected.draft)" maxlength="32000" rows="12" /></label>
      <p v-if="selected && !selected.draft" class="form-note">此技能没有可编辑的草稿。</p>
      <div class="form-actions"><button class="primary-button" :disabled="busy || !dirty || !name.trim() || !description.trim() || !content.trim()" @click="save">保存草稿</button><button v-if="selected" :disabled="busy || dirty || !selected.draft" @click="publish">发布</button></div>
      <div v-if="selected?.versions.length" class="versions"><h2>版本历史</h2><button v-for="version in selected.versions" :key="version.resource_id" type="button" @click="viewVersion(version.version)">版本 {{ version.version }}</button><pre v-if="historical">{{ historical }}</pre></div>
    </fieldset>
  </div></section>
</template>

<style scoped>
.library-toolbar{display:flex;align-items:end;gap:12px;flex-wrap:wrap}.library-toolbar label{flex:1;min-width:180px}.skill-list{list-style:none;padding:0;margin:16px 0;border-top:1px solid #e2e9ea}.skill-list li{border-bottom:1px solid #e2e9ea}.skill-list button{width:100%;border:0;background:transparent;text-align:left;padding:12px;display:flex;gap:12px;align-items:baseline;flex-wrap:wrap;cursor:pointer}.skill-list button:hover,.skill-list button[aria-current=true]{background:#eff8f6}.skill-list span{flex:1;min-width:120px;overflow-wrap:anywhere}.skill-list small{color:#61747a}.versions{margin-top:20px}.versions h2{font-size:15px}.versions button{margin-right:8px}.versions pre{white-space:pre-wrap;overflow-wrap:anywhere;padding:12px;background:#f4f7f7;border:1px solid #dce5e5}.qa-form fieldset .form-actions{margin-top:12px}
</style>
