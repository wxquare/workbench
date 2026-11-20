<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api, bearerToken } from '../session'
import { submitFeedback, type FeedbackSubmission, type FeedbackView } from '../feedback-api'

const props = defineProps<{ runId: string; terminal: boolean }>()
const reason = ref<FeedbackSubmission['reason']>('other')
const comment = ref('')
const busy = ref(false)
const result = ref<FeedbackView | null>(null)
const error = ref('')
let generation = 0
let key = ''
let signature = ''

const canSubmit = computed(() => props.terminal && !!props.runId.trim() && !!bearerToken.value && !busy.value)

watch(
  () => [props.runId, bearerToken.value],
  () => {
    generation++
    busy.value = false
    result.value = null
    error.value = ''
    reason.value = 'other'
    comment.value = ''
    key = ''
    signature = ''
  },
)

async function send(rating: 1 | -1) {
  if (!canSubmit.value) return
  const body: FeedbackSubmission = rating === 1
    ? {rating}
    : {rating, reason: reason.value, ...(comment.value.trim() ? {comment: comment.value.trim()} : {})}
  const request = JSON.stringify(body)
  if (request !== signature) {
    signature = request
    key = crypto.randomUUID()
  }
  const current = generation
  busy.value = true
  error.value = ''
  try {
    const value = await submitFeedback(api, props.runId, body, key)
    if (current !== generation) return
    result.value = value
    signature = ''
    key = ''
  } catch (e) {
    if (current === generation) error.value = e instanceof Error ? e.message : '反馈提交失败'
  } finally {
    if (current === generation) busy.value = false
  }
}
</script>

<template>
  <section v-if="terminal" class="run-feedback" aria-label="回答反馈">
    <p v-if="result" class="feedback-recorded" role="status">
      已记录反馈。
      <router-link
        v-if="result.knowledge_gap_id"
        :to="{name: 'knowledge', query: {gap: result.knowledge_gap_id}}"
      >查看知识缺口</router-link>
    </p>
    <template v-else>
      <div class="feedback-actions">
        <span>这个回答有帮助吗？</span>
        <button type="button" :disabled="!canSubmit" @click="send(1)">有用</button>
        <button type="button" :disabled="!canSubmit" @click="send(-1)">没用</button>
      </div>
      <div class="feedback-detail">
        <select v-model="reason" aria-label="反馈原因">
          <option value="irrelevant">答非所问</option>
          <option value="citation_error">引用错误</option>
          <option value="stale">资料过期</option>
          <option value="other">其他</option>
        </select>
        <input v-model="comment" maxlength="4000" placeholder="补充说明（可选）" />
      </div>
      <p v-if="error" class="form-error" role="alert">{{ error }}</p>
    </template>
  </section>
</template>

<style scoped>
.run-feedback { margin-top: 20px; border-top: 1px solid #e4e9ed; padding-top: 16px; }
.feedback-actions, .feedback-detail { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.feedback-actions span { margin-right: auto; color: #586773; }
.feedback-detail { margin-top: 10px; }
.feedback-detail input { flex: 1; min-width: 180px; }
.feedback-recorded { color: #2f6b4f; }
</style>
