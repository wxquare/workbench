<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { onBeforeRouteLeave, useRoute, useRouter } from 'vue-router'
import { api, bearerToken } from '../session'
import { agentError } from '../agents-api'
import { KnowledgeApi, type EntryView, type KBDraft, type KnowledgeKind, type PublishedVersion, type SearchHit } from '../knowledge-api'
import { draftBytes, markdownBlocks } from '../knowledge-markdown'
import KnowledgeGaps from '../components/KnowledgeGaps.vue'

const router = useRouter(), route = useRoute()
let service = new KnowledgeApi(api), generation = 0, searchGeneration = 0
const entries = ref<EntryView[]>([]), selected = ref<EntryView | null>(null), versions = ref<PublishedVersion[]>([])
const creating = ref(false), busy = ref(false), loading = ref(false), error = ref(''), notice = ref('')
const tab = ref<'entries' | 'search' | 'gaps'>(route.query.gap ? 'gaps' : 'entries'), query = ref(''), kindFilter = ref(''), statusFilter = ref('')
const title = ref(''), kind = ref<KnowledgeKind>('faq'), body = ref(''), scopeService = ref(''), environment = ref(''), region = ref('')
const tags = ref(''), aliases = ref(''), reviewBy = ref(''), baseline = ref(''), preview = ref(false), showHistory = ref(false)
const debugQuery = ref(''), debugService = ref(''), debugEnvironment = ref(''), debugRegion = ref('')
const hits = ref<SearchHit[]>([]), searched = ref(false), searching = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
const pendingImport = ref<{ bytes: Uint8Array; filename: string; body: string } | null>(null)
const typeNames: Record<KnowledgeKind, string> = { faq: 'FAQ', runbook: 'Runbook', glossary: '术语', case: '历史案例' }
const draft = computed<KBDraft>(() => ({ title: title.value.trim(), kind: kind.value, body_md: body.value,
  service: scopeService.value.trim() || null, environment: environment.value.trim() || null, region: region.value.trim() || null,
  tags: tags.value.split(',').map(x => x.trim()).filter(Boolean), aliases: aliases.value.split(',').map(x => x.trim()).filter(Boolean), review_by: reviewBy.value || null }))
const snapshot = computed(() => JSON.stringify(draft.value))
const dirty = computed(() => snapshot.value !== baseline.value)
const valid = computed(() => !!draft.value.title && !!draft.value.body_md.trim() && draftBytes(body.value) <= 16384 &&
  (draft.value.tags?.length ?? 0) <= 16 && (draft.value.aliases?.length ?? 0) <= 16)
const filtered = computed(() => entries.value.filter(item => (!query.value || [item.title, ...(item.tags || []), ...(item.aliases || [])].join(' ').toLowerCase().includes(query.value.toLowerCase())) &&
  (!kindFilter.value || item.kind === kindFilter.value) && (!statusFilter.value || (statusFilter.value === 'changes' ? item.has_unpublished_changes : item.status === statusFilter.value))))
const blocks = computed(() => markdownBlocks(body.value))
const editing = computed(() => creating.value || selected.value !== null)
function canLeave() { return !editing.value || (!dirty.value && !pendingImport.value) || window.confirm('当前草稿有未保存的修改，是否放弃？') }
onBeforeRouteLeave(() => canLeave())
function beforeUnload(event: BeforeUnloadEvent) { if (editing.value && (dirty.value || pendingImport.value)) { event.preventDefault(); event.returnValue = '' } }
if (typeof window !== 'undefined' && window.addEventListener) window.addEventListener('beforeunload', beforeUnload)
function clearDraft() { pendingImport.value = null; title.value = body.value = scopeService.value = environment.value = region.value = tags.value = aliases.value = reviewBy.value = ''; kind.value = 'faq'; baseline.value = snapshot.value }
function show(item: EntryView) {
  selected.value = item; creating.value = false; showHistory.value = false; preview.value = false
  const d = item.draft
  title.value = d?.title ?? item.title; kind.value = d?.kind ?? item.kind; body.value = d?.body_md ?? ''
  scopeService.value = d?.service ?? ''; environment.value = d?.environment ?? ''; region.value = d?.region ?? ''
  tags.value = d?.tags?.join(', ') ?? ''; aliases.value = d?.aliases?.join(', ') ?? ''; reviewBy.value = d?.review_by ?? ''
  baseline.value = snapshot.value
}
async function load() {
  const current = generation; loading.value = true
  try { const result = await service.allEntries(); if (current === generation) entries.value = result }
  catch (e) { if (current === generation) error.value = agentError(e) }
  finally { if (current === generation) loading.value = false }
}
async function open(id: string) {
  if (!canLeave()) return
  const current = ++generation; pendingImport.value = null; loading.value = true; error.value = notice.value = ''
  try {
    const item = await service.get(id)
    if (current !== generation) return
    if (!item.draft && item.published_version) { goVersion(item.entry_id, item.published_version); return }
    show(item)
  }
  catch (e) { if (current === generation) error.value = agentError(e) }
  finally { if (current === generation) loading.value = false }
}
function startCreate() {
  if (!canLeave()) return
  ++generation; selected.value = null; creating.value = true; clearDraft(); error.value = notice.value = ''; preview.value = false
}
async function startImportNew() {
  if (!canLeave()) return
  startCreate()
  await nextTick()
  fileInput.value?.click()
}
function back() {
  if (!canLeave()) return
  ++generation; selected.value = null; creating.value = false; clearDraft(); error.value = notice.value = ''; void load()
}
async function save() {
  if (!valid.value || busy.value) return
  const current = generation; busy.value = true; error.value = notice.value = ''
  try {
    let result = selected.value
    if (creating.value || dirty.value) {
      result = creating.value ? await service.create(draft.value) : await service.save(selected.value!.entry_id, selected.value!.revision, draft.value)
      if (current !== generation) return
      show(result)
    }
    if (pendingImport.value && result) {
      const file = pendingImport.value
      result = await service.importText(result.entry_id, result.revision, file.bytes, file.filename)
      if (current !== generation) return
      pendingImport.value = null; show(result)
    }
    notice.value = '草稿已保存；当前发布版本不变。'; await load()
  } catch (e) { if (current === generation) error.value = agentError(e) }
  finally { if (current === generation) busy.value = false }
}
async function publish() {
  if (!selected.value || dirty.value || pendingImport.value || !valid.value || !window.confirm(`发布「${selected.value.title}」的当前草稿？`)) return
  const current = generation, item = selected.value; busy.value = true; error.value = notice.value = ''
  try {
    const result = await service.publish(item.entry_id, item.revision)
    if (current !== generation) return
    notice.value = `已发布版本 v${result.version}。`
    try { const updated = await service.get(item.entry_id); if (current === generation) { show(updated); await load() } }
    catch (e) { if (current === generation) error.value = `发布成功，但刷新失败：${agentError(e)}` }
  } catch (e) { if (current === generation) error.value = agentError(e) }
  finally { if (current === generation) busy.value = false }
}
async function archive() {
  if (!selected.value || !window.confirm('归档后不再参与新检索，历史版本仍可访问。确认归档？')) return
  const current = generation, item = selected.value; busy.value = true
  try { const result = await service.archive(item.entry_id, item.revision); if (current === generation) { show(result); notice.value = '条目已归档。'; await load() } }
  catch (e) { if (current === generation) error.value = agentError(e) }
  finally { if (current === generation) busy.value = false }
}
async function history() {
  if (!selected.value) return
  const current = generation
  try { const page = await service.allVersions(selected.value.entry_id); if (current === generation) { versions.value = page; showHistory.value = true } }
  catch (e) { if (current === generation) error.value = agentError(e) }
}
async function rollback(version: number) {
  if (!selected.value || dirty.value || !window.confirm(`将 v${version} 重新发布为新版本？当前草稿将被替换。`)) return
  const current = generation, item = selected.value; busy.value = true
  try { const result = await service.rollback(item.entry_id, version, item.revision); if (current !== generation) return
    const updated = await service.get(item.entry_id)
    if (current === generation) { show(updated); notice.value = `已重新发布为 v${result.version}。`; await load() } }
  catch (e) { if (current === generation) error.value = agentError(e) }
  finally { if (current === generation) busy.value = false }
}
async function importFile(event: Event) {
  const input = event.target as HTMLInputElement, file = input.files?.[0]
  if (!file) return
  const current = generation
  try {
    if (!/\.(md|markdown|txt)$/i.test(file.name) || file.size > 16387) throw new Error('请选择不超过 16 KiB 的 Markdown 或文本文件。')
    const bytes = new Uint8Array(await file.arrayBuffer())
    if (current !== generation) return
    const text = new TextDecoder('utf-8', { fatal: true }).decode(bytes).replace(/\r\n?/g, '\n')
    if (!text.trim() || draftBytes(text) > 16384 || text.includes('\0')) throw new Error('文件为空、超出限制或包含无效文本。')
    body.value = text
    pendingImport.value = { bytes, filename: file.name, body: text }
    if (creating.value && !title.value) title.value = file.name.replace(/\.(md|markdown|txt)$/i, '')
    notice.value = '文件已载入草稿，保存后生效。'
  } catch (e) { if (current === generation) error.value = e instanceof Error ? e.message : '无法读取文件。' }
  finally { input.value = '' }
}
async function searchKnowledge() {
  if (!debugQuery.value.trim() || !debugService.value.trim() || !debugEnvironment.value.trim() || !debugRegion.value.trim()) return
  const current = generation, search = ++searchGeneration; searching.value = true; error.value = ''; searched.value = false; hits.value = []
  try { const result = await service.search(debugQuery.value, { service: debugService.value, environment: debugEnvironment.value, region: debugRegion.value }); if (current === generation && search === searchGeneration) { hits.value = result.matches; searched.value = true } }
  catch (e) { if (current === generation && search === searchGeneration) error.value = agentError(e) }
  finally { if (current === generation && search === searchGeneration) searching.value = false }
}
function goVersion(id: string, version: number, chunk?: string) { void router.push({ name: 'knowledge-version', params: { id, version: String(version) }, hash: chunk ? `#${chunk}` : '' }) }
watch(() => route.query.gap, gap => { if (typeof gap === 'string' && gap) tab.value = 'gaps' })
watch(bearerToken, () => {
  ++generation; service.reset(); service = new KnowledgeApi(api); entries.value = []; selected.value = null; creating.value = false
  clearDraft(); versions.value = []; hits.value = []; searched.value = false; error.value = notice.value = ''; busy.value = loading.value = searching.value = false
  if (bearerToken.value) void load()
}, { immediate: true })
watch(body, value => { if (pendingImport.value && pendingImport.value.body !== value) pendingImport.value = null })
onUnmounted(() => { ++generation; if (typeof window !== 'undefined' && window.removeEventListener) window.removeEventListener('beforeunload', beforeUnload) })
</script>

<template>
  <div v-if="!editing" class="kb-page">
    <header class="kb-heading"><div><h1>知识库</h1><p>把团队经验，变成 Agent 可引用的知识。</p></div><div class="kb-actions"><button :disabled="!bearerToken || loading" @click="startImportNew">导入文件</button><button class="kb-primary" :disabled="!bearerToken || loading" @click="startCreate">＋ 新建条目</button></div></header>
    <p v-if="!bearerToken" class="form-note">请先提供 Bearer Token。</p>
    <p v-if="error" class="form-error" role="alert">{{ error }}</p><p v-if="notice" class="form-note" role="status">{{ notice }}</p>
    <nav class="kb-tabs" aria-label="知识库页面"><button :class="{ active: tab === 'entries' }" @click="tab = 'entries'">知识条目 <small>{{ entries.length }}</small></button><button :class="{ active: tab === 'gaps' }" @click="tab = 'gaps'">知识缺口</button><button :class="{ active: tab === 'search' }" @click="tab = 'search'">检索调试</button></nav>
    <section v-if="tab === 'entries'" class="kb-panel">
      <div class="kb-filters"><input v-model="query" aria-label="搜索标题、标签或别名" placeholder="搜索标题、标签或别名…" /><select v-model="kindFilter" aria-label="类型筛选"><option value="">全部类型</option><option v-for="(label, value) in typeNames" :key="value" :value="value">{{ label }}</option></select><select v-model="statusFilter" aria-label="状态筛选"><option value="">全部状态</option><option value="published">已发布</option><option value="changes">有未发布修改</option><option value="draft">草稿</option><option value="archived">已归档</option></select></div>
      <div v-if="loading" class="kb-empty">加载中…</div><div v-else-if="!filtered.length" class="kb-empty">没有符合筛选条件的条目。</div>
      <div v-else class="kb-table-wrap"><table><thead><tr><th>条目</th><th>类型</th><th>适用范围</th><th>发布状态</th><th>最近更新</th></tr></thead><tbody><tr v-for="item in filtered" :key="item.entry_id"><td><button class="kb-link kb-item-title" :disabled="busy" @click="open(item.entry_id)">{{ item.title }}</button><small>{{ item.owner }}</small></td><td>{{ typeNames[item.kind] }}</td><td>{{ item.service || '全部服务' }}<small>{{ item.environment || '全部环境' }} / {{ item.region || '全部区域' }}</small></td><td><span class="kb-badge" :class="item.status">{{ item.status === 'published' ? `已发布 v${item.published_version}` : item.status === 'draft' ? '草稿' : '已归档' }}</span><small v-if="item.has_unpublished_changes" class="kb-unpublished">有未发布修改</small></td><td>{{ new Date(item.updated_at).toLocaleDateString() }}</td></tr></tbody></table></div>
      <div class="kb-panel-footer">共 {{ filtered.length }} 个条目 <span>仅已发布版本参与 Agent 检索</span></div>
    </section>
    <KnowledgeGaps v-else-if="tab === 'gaps'" :gap-id="typeof route.query.gap === 'string' ? route.query.gap : undefined" @edit="show" />
    <section v-else class="kb-panel kb-search-panel"><h2>检索调试</h2><p>输入问题与明确范围，查看当前命中片段。</p><label>模拟 Agent 的问题<input v-model="debugQuery" placeholder="例如：库存扣减失败怎么排查？" /></label><div class="kb-scope-grid"><label>服务<input v-model="debugService" placeholder="stock" /></label><label>环境<input v-model="debugEnvironment" placeholder="test" /></label><label>区域<input v-model="debugRegion" placeholder="sg" /></label></div><button class="kb-primary" :disabled="searching || !debugQuery.trim() || !debugService.trim() || !debugEnvironment.trim() || !debugRegion.trim()" @click="searchKnowledge">测试检索</button><div v-if="searched" class="kb-results"><h3>命中片段 {{ hits.length }}</h3><p v-if="!hits.length">当前范围内没有匹配的已发布知识。</p><article v-for="hit in hits" :key="hit.source_id"><div class="kb-result-heading"><strong>{{ hit.title }}</strong><span class="kb-badge published">v{{ hit.version }}</span></div><p class="kb-quote">{{ hit.chunk.content }}</p><small>{{ hit.chunk.heading_path.join(' / ') || '正文' }} · 行 {{ hit.chunk.line_start }}–{{ hit.chunk.line_end }}</small><button class="kb-link" @click="goVersion(hit.entry_id, hit.version, hit.chunk.chunk_id)">查看原文 ↗</button></article></div></section>
    <p v-if="tab === 'entries'" class="kb-hint">保存草稿不会改变已发布内容。发布后，Agent 的下一次检索即可使用新版本。</p>
  </div>
  <div v-else class="kb-page"><button class="kb-link kb-back" @click="back">← 知识条目</button><header class="kb-heading"><div><h1>{{ creating ? '新建知识条目' : '编辑知识条目' }}</h1><p>{{ selected?.published_version ? `当前已发布 v${selected.published_version}` : '未发布' }} · {{ dirty || pendingImport ? '有未保存修改' : '草稿已保存' }}</p></div><div class="kb-actions"><button v-if="selected?.published_version" :disabled="busy" @click="history">版本历史</button><button :disabled="busy || !valid || (!dirty && !creating && !pendingImport)" @click="save">保存草稿</button><button class="kb-primary" :disabled="busy || dirty || !!pendingImport || !valid || !selected || (selected.status === 'published' && !selected.has_unpublished_changes)" @click="publish">发布版本</button></div></header>
    <p v-if="error" class="form-error" role="alert">{{ error }}</p><p v-if="notice" class="form-note" role="status">{{ notice }}</p>
    <fieldset class="kb-editor-grid" :disabled="busy || loading"><div class="kb-editor-main"><section class="kb-panel kb-pad"><label>条目标题<input v-model="title" maxlength="200" placeholder="输入条目标题" /></label></section><section class="kb-panel"><div class="kb-panel-heading"><strong>Markdown 正文</strong><div><button class="kb-link" @click="preview = false">编辑</button><button class="kb-link" @click="preview = true">预览</button></div></div><label v-if="!preview" class="kb-editor-label"><span class="sr-only">Markdown 正文</span><textarea v-model="body" rows="14" placeholder="从一个问题、适用条件或排查步骤开始…" /></label><div v-else class="kb-preview"><component :is="block.kind === 'paragraph' ? 'p' : block.kind === 'code' ? 'pre' : block.kind" v-for="(block, index) in blocks" :key="index">{{ block.text }}</component></div><div class="kb-panel-footer"><span><button class="kb-link" @click="fileInput?.click()">从文件替换正文</button><input ref="fileInput" type="file" accept=".md,.markdown,.txt" hidden @change="importFile" /></span><span>{{ draftBytes(body) }} / 16,384 字节</span></div></section><p class="kb-hint">{{ selected?.published_version ? `当前检索继续使用 v${selected.published_version}；草稿在发布后生效。` : '草稿对 Agent 不可见，发布后才参与检索。' }}</p>
      <section v-if="showHistory" class="kb-panel kb-pad"><h2>版本历史</h2><div v-for="version in versions" :key="version.version" class="kb-version"><span>v{{ version.version }} · {{ version.author }}</span><div><button class="kb-link" @click="goVersion(selected?.entry_id || '', version.version)">查看</button><button v-if="version.version !== selected?.published_version" class="kb-link" @click="rollback(version.version)">重新发布</button></div></div></section></div>
      <aside class="kb-panel kb-pad kb-properties"><h2>条目属性</h2><label>知识类型<select v-model="kind"><option v-for="(label, value) in typeNames" :key="value" :value="value">{{ label }}</option></select></label><label>服务<input v-model="scopeService" placeholder="留空表示全部服务" /></label><label>环境<input v-model="environment" placeholder="留空表示全部环境" /></label><label>区域<input v-model="region" placeholder="留空表示全部区域" /></label><p>留空表示通用适用，不扩大查询权限。</p><label>标签<input v-model="tags" placeholder="用逗号分隔" /></label><label>检索别名<input v-model="aliases" placeholder="用逗号分隔" /></label><label>建议复查日期<input v-model="reviewBy" type="date" /></label><button v-if="selected?.published_version && selected.status !== 'archived'" @click="archive">归档条目</button></aside></fieldset>
  </div>
</template>

<style scoped>
.kb-page{color:#27344a}.kb-heading{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;margin:0 0 22px}.kb-heading h1{margin:0;font-size:24px}.kb-heading p,.kb-search-panel>p,.kb-properties p{font-size:12px;color:#8b99ac;margin:7px 0}.kb-actions{display:flex;gap:8px;flex-wrap:wrap}.kb-page button{border:1px solid #dfe6f0;border-radius:7px;background:white;padding:8px 12px;font:inherit;color:#45536b;cursor:pointer}.kb-page button:disabled{opacity:.5;cursor:not-allowed}.kb-page .kb-primary{background:#4669e8;color:#fff;border-color:#4669e8}.kb-page .kb-link{border:0;background:transparent;color:#506ed1;padding:4px 0}.kb-page .kb-item-title{font-weight:600;color:#27344a;text-align:left}.kb-back{margin-bottom:15px}.kb-tabs{display:flex;gap:24px;border-bottom:1px solid #e7ecf4;margin-bottom:20px}.kb-tabs button{border:0;border-bottom:2px solid transparent;border-radius:0;background:transparent;padding:0 0 11px;color:#8795a8}.kb-tabs button.active{border-bottom-color:#4669e8;color:#4669e8}.kb-tabs small{font-size:10px}.kb-panel{background:#fff;border:1px solid #e7ecf4;border-radius:9px;min-width:0}.kb-filters{display:flex;gap:8px;padding:16px;flex-wrap:wrap}.kb-filters input{flex:1;min-width:180px}.kb-page input,.kb-page select,.kb-page textarea{border:1px solid #dfe6f0;border-radius:6px;padding:9px 10px;color:#27344a;background:white;font:inherit;min-width:0;max-width:100%}.kb-page label{display:grid;gap:7px;font-size:11px;color:#536176}.kb-empty{padding:45px;text-align:center;color:#8b99ac}.kb-table-wrap{overflow-x:auto}.kb-page table{width:100%;border-collapse:collapse;white-space:normal}.kb-page th{background:#fafbfe;color:#9ba9ba;font-size:10px;font-weight:500;padding:11px 16px;border-block:1px solid #edf0f5}.kb-page td{font-size:11px;padding:15px 16px;border-bottom:1px solid #edf0f5}.kb-page td small{display:block;color:#9ba9ba;margin-top:4px}.kb-badge{display:inline-block;background:#f1f4f8;color:#78869a;border-radius:4px;padding:3px 7px;font-size:10px}.kb-badge.published{background:#eaf8f1;color:#328b66}.kb-badge.draft,.kb-unpublished{color:#b18349}.kb-panel-footer{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;padding:12px 16px;font-size:10px;color:#9ba9ba}.kb-hint{font-size:11px;color:#8997a9;margin:15px 0}.kb-editor-grid{display:grid;grid-template-columns:minmax(0,1fr) 210px;gap:15px;border:0;padding:0;margin:0;min-width:0}.kb-editor-main{display:grid;gap:15px;min-width:0}.kb-pad{padding:16px}.kb-pad input,.kb-pad select{width:100%}.kb-panel-heading{display:flex;justify-content:space-between;padding:12px 16px;border-bottom:1px solid #edf0f5;font-size:12px}.kb-panel-heading button+button,.kb-version button+button{margin-left:12px}.kb-editor-label textarea{width:100%;resize:vertical;border:0;border-radius:0;padding:16px;font:12px/1.9 monospace}.kb-preview{min-height:260px;padding:16px;font-size:12px;line-height:1.8;overflow-wrap:anywhere}.kb-preview pre,.kb-quote{white-space:pre-wrap;overflow-wrap:anywhere;background:#f5f8ff;padding:12px}.kb-properties h2,.kb-search-panel h2,.kb-pad h2{font-size:14px;margin:0 0 16px}.kb-properties label{margin-bottom:14px}.kb-properties p{margin:0 0 16px}.kb-version,.kb-result-heading{display:flex;justify-content:space-between;gap:12px;padding:10px 0}.kb-search-panel{padding:18px}.kb-search-panel>label{margin:18px 0}.kb-scope-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:16px}.kb-results{margin-top:20px;border-top:1px solid #edf0f5}.kb-results h3{font-size:13px}.kb-results article{border-top:1px solid #edf0f5;padding:10px 0}.kb-results article small{color:#8b99ac;margin-right:12px}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}@media(max-width:760px){.kb-editor-grid,.kb-scope-grid{grid-template-columns:1fr}.kb-filters input,.kb-filters select{width:100%}.kb-heading{align-items:flex-start}.kb-table-wrap{overflow-x:auto}.kb-page table{min-width:620px}}
</style>
