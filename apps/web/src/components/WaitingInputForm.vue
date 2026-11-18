<script setup lang="ts">
import { ref, watch } from 'vue'
import type { WaitingModel } from '../run-detail-state'
const props = defineProps<{ waiting: Extract<WaitingModel, { kind: 'input' }>; disabled: boolean }>()
const emit = defineEmits<{ submit: [region: string] }>()
const region = ref('')
watch(() => [props.waiting.interruptId, props.waiting.revision], () => { region.value = '' })
</script>
<template>
  <form class="summary-box" @submit.prevent="region.trim() && emit('submit', region)">
    <h3>任务需要补充信息</h3>
    <p v-if="waiting.scope">范围：{{ waiting.scope }}</p>
    <label>区域 <input v-model="region" required :disabled="disabled" placeholder="例如 sg"></label>
    <button type="submit" :disabled="disabled || !region.trim()">提交</button>
  </form>
</template>
