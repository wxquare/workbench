<script setup lang="ts">
defineProps<{ text: string }>()
</script>
<template>
  <section class="summary-box" aria-live="polite"><h3>生成中（草稿，未经验证）</h3><p class="result-text">{{ text }}</p></section>
</template>
