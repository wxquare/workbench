<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import type { SkillFiles } from '../skills-api'
import { filesError, imageMime, pathError, readLocalFile } from '../skill-files'

const props = withDefaults(defineProps<{ modelValue: SkillFiles; disabled?: boolean; readonly?: boolean }>(), { disabled: false, readonly: false })
const emit = defineEmits<{ 'update:modelValue': [files: SkillFiles]; uploading: [value: boolean] }>()
const selected = ref('SKILL.md'), path = ref(''), error = ref(''), uploading = ref(false)
let uploadGeneration = 0
const paths = computed(() => Object.keys(props.modelValue).sort((a, b) => a === 'SKILL.md' ? -1 : b === 'SKILL.md' ? 1 : a.localeCompare(b)))
const file = computed(() => props.modelValue[selected.value])
const locked = computed(() => props.disabled || props.readonly || uploading.value)
const image = computed(() => file.value?.encoding === 'base64' && imageMime(selected.value) ? `data:${imageMime(selected.value)};base64,${file.value.content}` : '')
watch(() => props.modelValue, () => { if (!file.value) select(paths.value[0] ?? 'SKILL.md') })
watch(() => props.disabled, value => { if (value) { ++uploadGeneration; uploading.value = false; emit('uploading', false) } })
onUnmounted(() => { ++uploadGeneration })

function select(value: string) { selected.value = value; path.value = ''; error.value = '' }
function replace(files: SkillFiles, target = selected.value) {
  emit('update:modelValue', files); selected.value = target; path.value = ''; error.value = ''
}
function add() {
  if (locked.value) return
  const target = path.value.trim(), message = pathError(target, props.modelValue)
  if (message) { error.value = message; return }
  if (imageMime(target)) { error.value = '图片文件请通过上传添加。'; return }
  replace({ ...props.modelValue, [target]: { encoding: 'utf-8', content: '' } }, target)
}
function rename() {
  if (locked.value || selected.value === 'SKILL.md') return
  const target = path.value.trim(), message = pathError(target, props.modelValue, selected.value)
  if (message) { error.value = message; return }
  if (target === selected.value) { path.value = ''; error.value = ''; return }
  if (imageMime(target) !== imageMime(selected.value)) { error.value = '重命名须保留文件类型。'; return }
  const files = { ...props.modelValue, [target]: { ...file.value! } }; delete files[selected.value]
  replace(files, target)
}
function remove() {
  if (locked.value || selected.value === 'SKILL.md') return
  const files = { ...props.modelValue }; delete files[selected.value]
  replace(files, 'SKILL.md')
}
function edit(value: string) {
  if (locked.value || !file.value || file.value.encoding !== 'utf-8') return
  emit('update:modelValue', { ...props.modelValue, [selected.value]: { encoding: 'utf-8', content: value } })
}
function onInput(event: Event) { edit((event.target as HTMLTextAreaElement).value) }
async function upload(event: Event) {
  const input = event.target as HTMLInputElement, local = input.files?.[0]
  if (!local || locked.value) return
  const target = path.value.trim() || local.name
  // An explicit path may replace the selected resource. Other existing paths require selection first.
  const replacing = target === selected.value && !!file.value ? selected.value : undefined
  const message = pathError(target, props.modelValue, replacing)
  if (message) { error.value = message; input.value = ''; return }
  const current = ++uploadGeneration
  uploading.value = true; emit('uploading', true); error.value = ''
  try {
    const result = await readLocalFile(local, target)
    if (current !== uploadGeneration) return
    const files = { ...props.modelValue, [target]: result }, validation = filesError(files)
    // A new package may not yet have its main instruction filled in.
    if (validation && validation !== '请填写 SKILL.md 的技能指令。') throw new Error(validation)
    replace(files, target)
  } catch (e) { if (current === uploadGeneration) error.value = e instanceof Error ? e.message : '读取文件失败。' }
  finally {
    if (current === uploadGeneration) { uploading.value = false; emit('uploading', false); input.value = '' }
  }
}
</script>

<template>
  <div class="skill-files-editor" :aria-label="readonly ? '历史版本文件' : '草稿文件'">
    <div class="file-summary">{{ readonly ? '版本文件' : '草稿文件' }} · {{ paths.length }} 个</div>
    <nav class="file-tabs" aria-label="选择文件"><button v-for="item in paths" :key="item" type="button" :aria-current="selected === item ? 'true' : undefined" :disabled="disabled || uploading" @click="select(item)">{{ item }}</button></nav>
    <div v-if="!readonly" class="file-controls">
      <label>文件路径<input v-model="path" :disabled="locked" maxlength="256" placeholder="例如 references/errors.md；上传时可留空使用文件名" /></label>
      <div class="file-actions"><button type="button" :disabled="locked" @click="add">新建文件</button><button type="button" :disabled="locked || selected === 'SKILL.md'" @click="rename">重命名文件</button><button type="button" :disabled="locked || selected === 'SKILL.md'" @click="remove">删除文件</button></div>
      <label>上传或替换文件<input type="file" :disabled="locked" @change="upload" /></label>
      <p class="form-note">支持文本、脚本和 PNG/JPEG/GIF/WebP 图片。填写当前文件路径可替换文件；脚本仅作为资源保存。</p>
    </div>
    <p v-if="error" role="alert" class="form-error">{{ error }}</p>
    <p v-if="uploading" role="status" class="form-note">读取文件中…</p>
    <template v-if="file">
      <label v-if="file.encoding === 'utf-8'">{{ selected === 'SKILL.md' && !readonly ? '技能指令' : '文件内容' }}<textarea :value="file.content" :readonly="readonly" :disabled="disabled || uploading" :maxlength="selected === 'SKILL.md' && !readonly ? 32000 : undefined" rows="14" spellcheck="false" @input="onInput" /></label>
      <div v-else class="image-preview"><p>{{ selected }}</p><img v-if="image" :src="image" :alt="selected" /><p v-else>此资源无法预览。</p></div>
      <p v-if="selected === 'SKILL.md' && !readonly" class="form-note">这里编辑主指令正文，技能名称和描述会在发布时自动加入。</p>
    </template>
  </div>
</template>

<style scoped>
.skill-files-editor{margin:20px 0;padding:16px;border:1px solid #dce5f4;border-radius:8px;background:#fbfcff}.file-summary{font-size:13px;font-weight:600;color:#354963;margin-bottom:10px}.file-tabs,.file-actions{display:flex;flex-wrap:wrap;gap:7px;margin:10px 0}.file-tabs button,.file-actions button{padding:7px 10px;border:1px solid #d5deeb;background:#fff;border-radius:6px;color:#425672;cursor:pointer;overflow-wrap:anywhere}.file-tabs button[aria-current=true]{background:#edf2ff;border-color:#4b6fd6}.file-tabs button:disabled,.file-actions button:disabled{opacity:.5;cursor:not-allowed}.file-controls label{display:grid;gap:6px;margin:12px 0}.skill-files-editor label{display:grid;gap:6px}.skill-files-editor textarea{width:100%;box-sizing:border-box;resize:vertical;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}.image-preview img{display:block;max-width:100%;max-height:420px;object-fit:contain;border:1px solid #dce5f4;background:#fff}.form-note{font-size:12px;color:#6b7280}
</style>
