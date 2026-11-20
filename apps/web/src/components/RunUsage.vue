<script setup lang="ts">
import { computed } from 'vue'
import { formatUsageCost, type UsageSummary } from '../usage'

const props = defineProps<{ usage: UsageSummary | null }>()
const tokens = computed(() => {
  const value = props.usage
  if (!value) return '—'
  return `${value.input_tokens ?? '—'} / ${value.output_tokens ?? '—'}`
})
</script>

<template>
  <section v-if="usage" class="run-usage">
    <h2>用量与费用</h2>
    <dl>
      <div><dt>模型调用</dt><dd>{{ usage.model_calls ?? '—' }}</dd></div>
      <div><dt>输入 / 输出 tokens</dt><dd>{{ tokens }}</dd></div>
      <div><dt>费用</dt><dd>{{ formatUsageCost(usage) }}</dd></div>
    </dl>
  </section>
</template>
