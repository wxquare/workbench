<script setup lang="ts">
defineProps<{ disabled: boolean; state?: string }>()
const emit = defineEmits<{ reconcile: [] }>()
</script>

<template>
  <section class="panel detail-panel">
    <h2>核对投递结果</h2>
    <p>此操作只读取部署发送器的回执，不会重新发送外部动作。</p>
    <p v-if="state">当前状态：{{ state }}</p>
    <button type="button" :disabled="disabled" @click="emit('reconcile')">查询回执</button>
  </section>
</template>
