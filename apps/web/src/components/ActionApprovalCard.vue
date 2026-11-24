<script setup lang="ts">
const props = defineProps<{
  detail: {
    arguments: Record<string, unknown>
    target: Record<string, unknown>
    payload_hash: string
  }
  disabled: boolean
}>()
const emit = defineEmits<{
  decide: [decision: 'approve' | 'reject', expectedArgsHash: string]
}>()
</script>

<template>
  <section class="panel detail-panel">
    <h2>外部动作审批</h2>
    <dl>
      <div><dt>目标</dt><dd>{{ JSON.stringify(detail.target) }}</dd></div>
      <div><dt>参数</dt><dd><pre>{{ JSON.stringify(detail.arguments, null, 2) }}</pre></dd></div>
      <div><dt>参数哈希</dt><dd><code>{{ detail.payload_hash }}</code></dd></div>
    </dl>
    <div class="actions">
      <button type="button" :disabled="disabled" @click="emit('decide', 'approve', detail.payload_hash)">批准</button>
      <button type="button" :disabled="disabled" @click="emit('decide', 'reject', detail.payload_hash)">拒绝</button>
    </div>
  </section>
</template>

<style scoped>
pre { white-space: pre-wrap; overflow-wrap: anywhere; }
code { overflow-wrap: anywhere; }
.actions { display: flex; gap: 8px; }
</style>
