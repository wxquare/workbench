<script setup lang="ts">
import type { WaitingModel } from '../run-detail-state'
const props = defineProps<{ waiting: Extract<WaitingModel, { kind: 'approval' }>; disabled: boolean; question: string }>()
const emit = defineEmits<{ decide: [decision: 'approve' | 'reject'] }>()
function approve() { if (window.confirm('确认批准此等待？原始提问仅为上下文，不能代表审批内容的完整描述。')) emit('decide', 'approve') }
async function copyHash() { try { await navigator.clipboard.writeText(props.waiting.argsHash) } catch { /* hash remains selectable */ } }
</script>
<template>
  <section class="summary-box">
    <h3>任务等待审批</h3>
    <p>原始提问（仅供参考，不代表审批内容的完整描述）：{{ question || '不可用' }}</p>
    <p>审批 ID：{{ waiting.approvalId }}</p><p>发起人：{{ waiting.actor || '未知' }}</p>
    <p>参数哈希：<code :title="waiting.argsHash">{{ waiting.argsHash.slice(0, 12) }}</code> <button type="button" @click="copyHash">复制完整哈希</button></p>
    <button type="button" :disabled="disabled" @click="approve">批准</button>
    <button type="button" :disabled="disabled" @click="emit('decide', 'reject')">拒绝</button>
  </section>
</template>
