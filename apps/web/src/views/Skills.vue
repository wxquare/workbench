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
  <section class="admin-page skills-admin">
    <header class="admin-page-header"><div><p class="workflow-eyebrow">SKILL LIBRARY</p><h1>Skills</h1><p>维护可复用的技能指令，发布版本后供 Agent 绑定。</p></div><span class="admin-page-count">{{ skills.length }} 个 Skill</span></header>
    <div class="admin-alerts">
    <p v-if="!bearerToken" class="form-note">请先提供 Bearer Token。</p>
    <p v-if="error" class="form-error" role="alert">{{ error }}</p>
    <p v-if="notice" class="form-note" role="status">{{ notice }}</p>
    </div>
    <div class="admin-workspace">
    <aside class="admin-rail panel qa-form" aria-label="Skill 列表与搜索">
    <div class="library-toolbar"><label>搜索 Skill<input v-model="search" placeholder="名称或描述" /></label><button class="primary-button" :disabled="!bearerToken || busy" @click="startCreate"><Plus class="skill-toolbar-icon" /> 新建 Skill</button><button :disabled="!bearerToken || busy" title="刷新" aria-label="刷新" @click="load"><Refresh class="skill-toolbar-icon" /> 刷新</button></div>
    <div class="admin-list-heading"><h2>技能列表</h2><span class="admin-list-count">{{ filtered.length }}</span></div>
    <p v-if="loading" class="form-note">加载中…</p><p v-else-if="bearerToken && !filtered.length" class="form-note">没有匹配的 Skill。</p>
    <ul class="skill-list"><li v-for="item in filtered" :key="item.skill_id"><button :disabled="busy" :aria-current="selected?.skill_id === item.skill_id ? 'true' : undefined" @click="open(item.skill_id)"><strong>{{ item.name }}</strong><span>{{ item.description }}</span><small class="admin-status" :class="item.latest_version ? 'published' : 'draft'">{{ item.latest_version ? `版本 ${item.latest_version.version}` : '未发布' }}</small></button></li></ul>
    </aside>
    <div class="admin-detail panel qa-form">
    <fieldset v-if="selected || creating" :disabled="loading || busy"><legend>{{ creating ? '新建 Skill' : selected?.name }}</legend>
      <div v-if="selected" class="admin-detail-meta"><span class="admin-status" :class="selected.latest_version ? 'published' : 'draft'">{{ selected.latest_version ? `版本 ${selected.latest_version.version}` : '未发布' }}</span><span>修订 {{ selected.revision }}</span></div>
      <label>技能标识<input v-model="name" :disabled="!creating || busy" maxlength="128" placeholder="例如 value-investing" /></label>
      <label>描述<textarea v-model="description" :disabled="busy || (!!selected && !selected.draft)" maxlength="1000" rows="3" /></label>
      <label>技能指令<textarea v-model="content" :disabled="busy || (!!selected && !selected.draft)" maxlength="32000" rows="12" /></label>
      <p v-if="selected && !selected.draft" class="form-note">此技能没有可编辑的草稿。</p>
      <div class="form-actions"><button class="primary-button" :disabled="busy || !dirty || !name.trim() || !description.trim() || !content.trim()" @click="save">保存草稿</button><button v-if="selected" :disabled="busy || dirty || !selected.draft" @click="publish">发布</button></div>
      <div v-if="selected?.versions.length" class="versions"><h2>版本历史</h2><button v-for="version in selected.versions" :key="version.resource_id" type="button" @click="viewVersion(version.version)">版本 {{ version.version }}</button><pre v-if="historical">{{ historical }}</pre></div>
    </fieldset>
    <div v-else class="admin-empty"><span class="admin-empty-mark">S</span><h2>选择一个 Skill</h2><p>在左侧打开技能，编辑指令或查看已发布的版本。</p></div>
    </div>
    </div>
  </section>
</template>

<style scoped>
.library-toolbar{display:flex;align-items:end;gap:8px;flex-wrap:wrap;padding-bottom:18px;border-bottom:1px solid #edf1f6}.library-toolbar label{flex-basis:100%}.library-toolbar button{display:inline-flex;align-items:center;gap:5px;min-height:36px;padding:0 10px;border:1px solid #d5deeb;border-radius:7px;background:#fff;color:#425672;cursor:pointer}.library-toolbar button:disabled{opacity:.55;cursor:not-allowed}.admin-list-heading{margin-top:17px}.skill-list{list-style:none;padding:0;margin:0}.skill-list li+li{margin-top:5px}.skill-list button{width:100%;border:1px solid transparent;border-radius:8px;background:transparent;text-align:left;padding:12px;display:grid;justify-items:start;gap:6px;cursor:pointer}.skill-list button:hover,.skill-list button[aria-current=true]{background:#edf2ff;border-color:#dce5fc}.skill-list strong{color:#354963;font-size:12px}.skill-list span{color:#5b6d85;font-size:11px;line-height:1.5;overflow-wrap:anywhere}.skill-list small{font-size:10px}.versions{margin-top:24px;padding-top:18px;border-top:1px solid #edf1f6}.versions h2{font-size:15px;color:#354963}.versions button{margin-right:8px;margin-bottom:8px;padding:7px 10px;border:1px solid #dce5f4;border-radius:6px;background:#f6f8fe;color:#4c67a6;cursor:pointer}.versions pre{white-space:pre-wrap;overflow-wrap:anywhere;padding:14px;background:#f7f9fd;border:1px solid #e2e8f2;border-radius:8px}.qa-form fieldset .form-actions{margin-top:16px}
.library-toolbar .primary-button{flex:1;background:#4b6fd6;border-color:#4b6fd6;color:#fff;font-weight:650}.skill-toolbar-icon{width:15px;height:15px}
</style>
