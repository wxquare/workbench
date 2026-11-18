<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { api, bearerToken } from '../session'
import { agentError } from '../agents-api'
import { KnowledgeApi, type EntryView, type KnowledgeGap } from '../knowledge-api'

const props = defineProps<{ gapId?: string }>()
const emit = defineEmits<{ edit: [entry: EntryView] }>()
const router = useRouter()
let service = new KnowledgeApi(api), generation = 0, targetPending = true
const items = ref<KnowledgeGap[]>([]), selected = ref<KnowledgeGap | null>(null), status = ref<'open' | 'resolved' | 'dismissed'>('open')
const busy = ref(false), loading = ref(false), error = ref(''), notice = ref(''), cursor = ref<string | null>(null)
const reasons: Record<string, string> = { 'kb.empty': '无知识命中', 'kb.unanswerable': '知识不足以回答', 'kb.invalid_citation': '引用校验未通过' }
const scope = computed(() => selected.value?.query_scope ? Object.values(selected.value.query_scope).join(' / ') : '范围不可用')
async function load(append = false) {
  const current = generation; loading.value = true; error.value = ''
  try {
    const target = !append && targetPending && props.gapId ? await service.getGap(props.gapId) : null
    if (current !== generation) return
    if (target && target.review_status !== status.value) { status.value = target.review_status; return }
    const page = await service.gaps(status.value, append ? cursor.value || undefined : undefined)
    if (current !== generation) return
    items.value = append ? [...items.value, ...page.items] : page.items
    if (target) targetPending = false
    if (target && !items.value.some(x => x.gap_id === target.gap_id)) items.value.unshift(target)
    cursor.value = page.next_cursor
    selected.value = items.value.find(x => x.gap_id === (target?.gap_id ?? selected.value?.gap_id)) ?? items.value[0] ?? null
  } catch (e) { if (current === generation) { items.value = []; selected.value = null; error.value = agentError(e) } }
  finally { if (current === generation) loading.value = false }
}
async function draft() {
  const gap = selected.value
  if (!gap || busy.value) return
  const current = generation; busy.value = true; error.value = notice.value = ''
  try {
    const entry = gap.resolved_entry_id ? await service.get(gap.resolved_entry_id) : await service.toDraft(gap.gap_id, gap.gap_revision)
    if (current === generation) emit('edit', entry)
  } catch (e) { if (current === generation) error.value = agentError(e) }
  finally { if (current === generation) busy.value = false }
}
async function replay() {
  const gap = selected.value
  if (!gap || busy.value || !window.confirm('使用原问题与范围创建一个新任务？')) return
  const current = generation; busy.value = true; error.value = notice.value = ''
  try { const result = await service.replayGap(gap.gap_id, gap.gap_revision); if (current === generation) void router.push({ name: 'run-detail', params: { id: result.run_id } }) }
  catch (e) { if (current === generation) error.value = agentError(e) }
  finally { if (current === generation) busy.value = false }
}
async function resolve() {
  const gap = selected.value
  if (!gap || !gap.resolved_entry_id || !gap.replay_run_id || busy.value) return
  const current = generation; busy.value = true; error.value = notice.value = ''
  try {
    const [entry, run] = await Promise.all([service.get(gap.resolved_entry_id), api.run(gap.replay_run_id)])
    if (current !== generation) return
    const prefix = `kb:${gap.resolved_entry_id}@`
    const cited = run.outcome?.knowledge_answer?.claims.flatMap(claim => claim.citations).find(citation => citation.source_id.startsWith(prefix))
    const match = cited?.source_id.match(/^kb:[^@]+@(\d+)#/)
    if (!match || !run.outcome || !['succeeded', 'partial', 'failed', 'cancelled', 'timed_out', 'expired'].includes(run.status)) {
      throw new Error('重放结果尚未提供该条目的有效引用，请先核对任务结果。')
    }
    const note = window.prompt(`请核对任务结果及「${entry.title}」v${match[1]}，填写人工确认说明：`)
    if (!note?.trim()) return
    await service.updateGap(gap.gap_id, gap.gap_revision, { status: 'resolved', note: note.trim(), entry_id: gap.resolved_entry_id,
      version: Number(match[1]), verification_run_id: gap.replay_run_id })
    if (current === generation) { notice.value = '已记录人工确认。'; await load() }
  } catch (e) { if (current === generation) error.value = agentError(e) }
  finally { if (current === generation) busy.value = false }
}
async function dismiss() {
  const gap = selected.value
  if (!gap || busy.value) return
  const note = window.prompt('请填写忽略理由：')
  if (!note?.trim()) return
  const current = generation; busy.value = true; error.value = notice.value = ''
  try { await service.updateGap(gap.gap_id, gap.gap_revision, { status: 'dismissed', note: note.trim() }); if (current === generation) { notice.value = '已记录忽略理由。'; await load() } }
  catch (e) { if (current === generation) error.value = agentError(e) }
  finally { if (current === generation) busy.value = false }
}
watch([bearerToken, status, () => props.gapId], (values, previous) => {
  if (!previous || values[0] !== previous[0] || values[2] !== previous[2]) targetPending = true
  ++generation; service.reset(); service = new KnowledgeApi(api); items.value = []; selected.value = null; cursor.value = null
  error.value = notice.value = ''; busy.value = loading.value = false
  if (bearerToken.value) void load()
}, { immediate: true })
onUnmounted(() => { ++generation })
</script>

<template>
  <div class="gaps"><p v-if="error" class="form-error" role="alert">{{ error }}</p><p v-if="notice" class="form-note" role="status">{{ notice }}</p><div class="gaps-grid"><section class="gap-panel"><header><strong>知识缺口</strong><label>状态 <select v-model="status"><option value="open">待处理</option><option value="resolved">已解决</option><option value="dismissed">已忽略</option></select></label></header><p v-if="loading" class="gap-empty">加载中…</p><p v-else-if="!items.length" class="gap-empty">暂无知识缺口。</p><ul><li v-for="item in items" :key="item.gap_id"><button :class="{ active: item.gap_id === selected?.gap_id }" @click="selected = item"><strong>{{ item.summary }}</strong><small>{{ reasons[item.problem_type] }} · {{ item.query_scope?.service || '范围不可用' }} · {{ new Date(item.created_at).toLocaleString() }}</small></button></li></ul><button v-if="cursor" class="gap-more" :disabled="loading" @click="load(true)">加载更多</button></section><aside v-if="selected" class="gap-panel gap-detail"><h2>处理缺口</h2><p>{{ selected.summary }}</p><dl><dt>原因</dt><dd>{{ reasons[selected.problem_type] }}</dd><dt>适用范围</dt><dd>{{ scope }}</dd><dt>原始任务</dt><dd><router-link :to="{ name: 'run-detail', params: { id: selected.run_id } }">查看原 Run ↗</router-link></dd></dl><div class="gap-step"><strong>1　补充知识</strong><p>转为草稿，补充适用条件与正文。</p><button :disabled="busy || selected.review_status !== 'open'" @click="draft">{{ selected.resolved_entry_id ? '继续编辑条目' : '转为条目草稿' }}</button></div><div class="gap-step"><strong>2　发布并重放问题</strong><p>创建新任务验证，保留原任务结果。</p><button :disabled="busy || selected.review_status !== 'open'" @click="replay">{{ selected.replay_run_id ? '再次重放' : '重放验证' }}</button> <router-link v-if="selected.replay_run_id" :to="{ name: 'run-detail', params: { id: selected.replay_run_id } }">查看验证结果 ↗</router-link></div><div class="gap-step"><strong>3　人工确认是否解决</strong><p>执行完成不会自动关闭缺口。</p><button :disabled="busy || !selected.replay_run_id || !selected.resolved_entry_id || selected.review_status !== 'open'" @click="resolve">确认已解决</button> <button :disabled="busy || selected.review_status !== 'open'" @click="dismiss">忽略</button><p v-if="selected.note">处理说明：{{ selected.note }}</p></div></aside></div><p class="gap-hint">工具不可用或权限不足不会记成知识缺口。</p></div>
</template>

<style scoped>
.gaps{color:#27344a}.gaps-grid{display:grid;grid-template-columns:minmax(0,1fr) 280px;gap:15px}.gap-panel{background:white;border:1px solid #e7ecf4;border-radius:9px;min-width:0}.gap-panel header{display:flex;justify-content:space-between;align-items:center;padding:14px 16px;border-bottom:1px solid #edf0f5}.gap-panel select,.gap-panel button{font:inherit}.gap-panel select{padding:5px;border:1px solid #e2e8f0;border-radius:5px;background:white}.gap-panel header label{font-size:11px;color:#8997a9}.gap-panel ul{list-style:none;padding:0;margin:0}.gap-panel li{border-bottom:1px solid #edf0f5}.gap-panel li button{display:block;width:100%;text-align:left;border:0;background:white;padding:16px;color:#34445d;cursor:pointer}.gap-panel li button.active{background:#edf2ff;border-left:3px solid #4669e8}.gap-panel li small{display:block;margin-top:7px;color:#91a0b3}.gap-empty{padding:30px;text-align:center;color:#8997a9}.gap-more{margin:10px}.gap-detail{padding:16px}.gap-detail h2{font-size:14px;margin:0 0 14px}.gap-detail p{font-size:12px}.gap-detail dl{display:grid;grid-template-columns:70px minmax(0,1fr);gap:9px;font-size:11px}.gap-detail dt{color:#8997a9}.gap-detail dd{margin:0;overflow-wrap:anywhere}.gap-detail a{color:#4669e8}.gap-step{border-top:1px solid #edf0f5;padding-top:14px;margin-top:14px;font-size:11px}.gap-step p{color:#8997a9;font-size:11px}.gap-detail button,.gap-more{border:1px solid #dfe6f0;border-radius:6px;background:white;padding:7px 10px;color:#4669e8;cursor:pointer}.gap-detail button:disabled{opacity:.5;cursor:not-allowed}.gap-hint{font-size:11px;color:#8997a9;margin:15px 0}@media(max-width:760px){.gaps-grid{grid-template-columns:1fr}}
</style>
