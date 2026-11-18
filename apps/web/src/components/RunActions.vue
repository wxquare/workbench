<script setup lang="ts">
defineProps<{ status: string; busy: boolean }>()
const emit = defineEmits<{ cancel: [] }>()
function confirmCancel() { if (window.confirm('确定取消此任务吗？')) emit('cancel') }
</script>
<template>
  <button v-if="['queued', 'running', 'waiting_input', 'waiting_approval'].includes(status)" type="button" :disabled="busy" @click="confirmCancel">取消任务</button>
  <button v-else-if="status === 'cancelling'" type="button" disabled>取消中…</button>
</template>
