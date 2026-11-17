<script setup lang="ts">
import { computed } from 'vue'
import { statusPresentation, type RunStatus, type DeliveryStatus } from '../demo'
const props = defineProps<{ status: RunStatus | DeliveryStatus }>()
const item = computed(() => statusPresentation[props.status])
</script>
<template><span class="status" :class="item.tone"><i></i>{{ item.label }}</span></template>
