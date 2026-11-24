<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ApiError } from '../api'
import { api, bearerToken } from '../session'
import {
  getReview,
  getReviewSummary,
  listReviews,
  REVIEW_TYPES,
  type ReviewDetail,
  type ReviewItem,
  type ReviewSummary,
  type ReviewType,
} from '../reviews-api'
import { decideAction, reconcileAction } from '../actions-api'
import ActionApprovalCard from '../components/ActionApprovalCard.vue'
import ReconcilePanel from '../components/ReconcilePanel.vue'
import { createIntentKeys } from '../run-detail-state'

const labels: Record<ReviewType, string> = {
  run_input: '补充输入',
  run_approval: '任务审批',
  action_approval: '动作审批',
  action_reapproval: '失败重审',
  action_reconcile: '投递对账',
}
const items = ref<ReviewItem[]>([])
const summary = ref<ReviewSummary | null>(null)
const summaryError = ref('')
const error = ref('')
const loading = ref(false)
const busy = ref(false)
const filter = ref<ReviewType | ''>('')
const state = ref<'pending' | 'expired'>('pending')
const next = ref<string | null>(null)
const selected = ref<ReviewDetail | null>(null)
const keys = createIntentKeys()
let visible = true
let timer: ReturnType<typeof setInterval> | undefined

const selectedAction = computed(() => {
  if (selected.value?.item.type !== 'action_approval') return null
  const detail = selected.value.detail
  if (
    typeof detail !== 'object'
    || !('arguments' in detail)
    || !('target' in detail)
    || !('payload_hash' in detail)
  ) return null
  return detail as {
    arguments: Record<string, unknown>
    target: Record<string, unknown>
    payload_hash: string
  }
})

function friendly(errorValue: unknown) {
  if (errorValue instanceof ApiError && errorValue.code === 'INVALID_CURSOR') {
    return '列表状态已变化，请刷新。'
  }
  if (errorValue instanceof ApiError && errorValue.status === 403) {
    return '当前账号没有审核权限。'
  }
  return '审核事项加载失败，请稍后重试。'
}

async function load(reset = false) {
  if (loading.value) return
  loading.value = true
  error.value = ''
  try {
    const page = await listReviews(api, {
      type: filter.value || undefined,
      state: state.value,
      limit: 20,
      after: reset ? undefined : next.value ?? undefined,
    })
    items.value = reset ? page.items : [...items.value, ...page.items]
    next.value = page.next_cursor
  } catch (value) {
    error.value = friendly(value)
  } finally {
    loading.value = false
  }
}

async function refreshSummary() {
  if (!visible || !bearerToken.value) return
  summaryError.value = ''
  try {
    summary.value = await getReviewSummary(api)
  } catch {
    summary.value = null
    summaryError.value = '审核计数暂时不可用'
  }
}

async function choose(item: ReviewItem) {
  error.value = ''
  try {
    selected.value = await getReview(api, item.item_id)
  } catch (value) {
    selected.value = null
    error.value = friendly(value)
  }
}

async function reloadAfterMutation() {
  selected.value = null
  next.value = null
  keys.clear()
  await Promise.all([load(true), refreshSummary()])
}

async function decide(decision: 'approve' | 'reject', hash: string) {
  if (busy.value || !selected.value) return
  const item = selected.value
  const approvalId = item.detail.approval_id
  if (typeof approvalId !== 'string') return
  const key = keys.key('review-decision', item.item.item_id, decision)
  busy.value = true
  error.value = ''
  try {
    await decideAction(api, approvalId, decision, hash)
    keys.clear()
    await reloadAfterMutation()
  } catch (value) {
    error.value = friendly(value)
    keys.clear()
    await Promise.all([load(true), refreshSummary()])
  } finally {
    busy.value = false
    void key
  }
}

async function reconcile() {
  if (busy.value || !selected.value?.item.action_id) return
  const item = selected.value
  const actionId = item.item.action_id
  if (!actionId) return
  const key = keys.key('review-reconcile', item.item.item_id, null)
  busy.value = true
  error.value = ''
  try {
    await reconcileAction(api, actionId, key)
    keys.clear()
    await reloadAfterMutation()
  } catch (value) {
    error.value = friendly(value)
    await Promise.all([load(true), refreshSummary()])
  } finally {
    busy.value = false
  }
}

function changeFilter() {
  selected.value = null
  next.value = null
  void load(true)
}

function setVisible(value: boolean) {
  visible = value
}

onMounted(() => {
  visible = document.visibilityState !== 'hidden'
  void Promise.all([load(true), refreshSummary()])
  timer = setInterval(() => {
    visible = document.visibilityState !== 'hidden'
    void refreshSummary()
  }, 30_000)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
  keys.clear()
})
</script>

<template>
  <div class="page-heading">
    <div>
      <div class="eyebrow">HUMAN REVIEW <span></span> 审核中心</div>
      <h1>待人工处理</h1>
      <p>输入、审批、重审与投递对账</p>
    </div>
    <strong v-if="summary">
      待处理 {{ summary.total }}
    </strong>
    <span v-else-if="summaryError">{{ summaryError }}</span>
  </div>

  <section class="panel">
    <label>
      类型
      <select v-model="filter" @change="changeFilter">
        <option value="">全部</option>
        <option v-for="kind in REVIEW_TYPES" :key="kind" :value="kind">
          {{ labels[kind] }}
        </option>
      </select>
    </label>
    <label>
      状态
      <select v-model="state" @change="changeFilter">
        <option value="pending">待处理</option>
        <option value="expired">已过期</option>
      </select>
    </label>
  </section>

  <p v-if="error" class="form-error" role="alert">{{ error }}</p>
  <p v-if="loading && !items.length">正在加载审核事项…</p>
  <p v-if="!loading && !items.length">当前没有需要处理的审核事项。</p>

  <div class="detail-grid">
    <section class="panel detail-panel">
      <button
        v-for="item in items"
        :key="item.item_id"
        type="button"
        class="review-row"
        :class="{ active: selected?.item.item_id === item.item_id }"
        @click="choose(item)"
      >
        <strong>{{ item.title }}</strong>
        <span>{{ labels[item.type] }}</span>
        <small v-if="item.due_at">截止 {{ new Date(item.due_at).toLocaleString() }}</small>
        <small v-if="item.expired">只读</small>
        <small v-if="item.simulated">本地模拟，不代表真实投递</small>
      </button>
      <button
        v-if="next"
        type="button"
        :disabled="loading"
        @click="load(false)"
      >
        加载更多
      </button>
    </section>

    <aside v-if="selected" class="panel detail-panel">
      <h2>{{ selected.item.title }}</h2>
      <ActionApprovalCard
        v-if="selected.item.type === 'action_approval' && selectedAction"
        :detail="selectedAction"
        :disabled="busy || selected.item.expired"
        @decide="decide"
      />
      <ReconcilePanel
        v-else-if="selected.item.type === 'action_reconcile'"
        :disabled="busy || selected.item.expired"
        @reconcile="reconcile"
      />
      <p v-else>请通过支持的任务流程处理该事项。</p>
      <router-link :to="selected.item.links.run">查看任务 →</router-link>
    </aside>
  </div>
</template>

<style scoped>
.review-row {
  width: 100%;
  display: grid;
  justify-items: start;
  gap: 4px;
  padding: 12px;
  border: 1px solid #dbe2e8;
  background: white;
  text-align: left;
}
.review-row.active {
  border-color: #0f766e;
  background: #f0fdfa;
}
</style>
