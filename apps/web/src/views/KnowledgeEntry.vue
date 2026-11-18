<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api, bearerToken } from '../session'
import { agentError } from '../agents-api'
import { KnowledgeApi, type VersionView } from '../knowledge-api'
import { markdownBlocks } from '../knowledge-markdown'

const route = useRoute(), router = useRouter()
let generation = 0
const item = ref<VersionView | null>(null), error = ref(''), loading = ref(false)
watch(() => [route.params.id, route.params.version, bearerToken.value], async () => {
  const current = ++generation
  item.value = null; error.value = ''; loading.value = false
  if (!bearerToken.value) { error.value = '请先提供 Bearer Token。'; return }
  const id = String(route.params.id || ''), version = Number(route.params.version)
  if (!id || !Number.isSafeInteger(version) || version < 1 || String(version) !== String(route.params.version)) {
    error.value = '版本号无效。'; return
  }
  loading.value = true
  try {
    const result = await new KnowledgeApi(api).version(id, version)
    if (current === generation) {
      item.value = result
      await nextTick()
      if (current === generation && route.hash && typeof document !== 'undefined') {
        document.getElementById(route.hash.slice(1))?.scrollIntoView()
      }
    }
  }
  catch (e) { if (current === generation) error.value = agentError(e) }
  finally { if (current === generation) loading.value = false }
}, { immediate: true })
watch(() => route.hash, async hash => {
  await nextTick()
  if (hash && item.value && typeof document !== 'undefined') document.getElementById(hash.slice(1))?.scrollIntoView()
})
</script>

<template>
  <div class="kb-reference"><button class="kb-back" @click="router.push({ name: 'knowledge' })">← 知识库</button>
    <p v-if="error" class="form-error" role="alert">{{ error }}</p><p v-if="loading" class="form-note">加载中…</p>
    <template v-if="item"><h1>{{ item.title }}</h1><p class="kb-meta">只读版本 v{{ item.version }} · {{ item.status === 'archived' ? '条目已归档，不参与当前检索' : '历史内容不会随草稿修改' }}</p>
      <div class="kb-layout"><article class="kb-panel"><div class="kb-banner">引用固定到此版本。后续编辑和发布不会改变本页原文。</div><section v-for="chunk in item.chunks" :id="chunk.chunk_id" :key="chunk.chunk_id" class="kb-chunk"><component :is="block.kind === 'paragraph' ? 'p' : block.kind === 'code' ? 'pre' : block.kind" v-for="(block, index) in markdownBlocks(chunk.content)" :key="index">{{ block.text }}</component></section></article><aside class="kb-panel kb-info"><h2>来源信息</h2><dl><dt>版本</dt><dd>v{{ item.version }}</dd><dt>发布者</dt><dd>{{ item.author }}</dd><dt>适用范围</dt><dd>{{ item.service || '全部服务' }} / {{ item.environment || '全部环境' }} / {{ item.region || '全部区域' }}</dd><dt>内容哈希</dt><dd>{{ item.content_hash }}</dd></dl></aside></div>
    </template>
  </div>
</template>

<style scoped>
.kb-reference{color:#27344a}.kb-back{border:0;background:transparent;color:#5675d7;padding:0;margin:0 0 18px;font:inherit;cursor:pointer}.kb-reference h1{font-size:24px;margin:0}.kb-meta{color:#8997a9;font-size:12px;margin:8px 0 22px}.kb-layout{display:grid;grid-template-columns:minmax(0,1fr) 205px;gap:15px}.kb-panel{background:#fff;border:1px solid #e7ecf4;border-radius:9px;padding:18px;min-width:0}.kb-banner{background:#edf2ff;color:#5675d7;padding:11px;border-radius:6px;font-size:11px;margin-bottom:20px}.kb-chunk{line-height:1.85;font-size:12px;overflow-wrap:anywhere;scroll-margin-top:20px}.kb-chunk:target{background:#f3f6ff}.kb-chunk pre{white-space:pre-wrap;background:#f6f8fc;padding:12px}.kb-info h2{font-size:14px;margin:0 0 16px}.kb-info dl{font-size:11px;margin:0}.kb-info dt{color:#9aa7ba;margin-top:12px}.kb-info dd{margin:3px 0 0;overflow-wrap:anywhere}@media(max-width:760px){.kb-layout{grid-template-columns:1fr}}
</style>
