<script setup lang="ts">
import { ArrowRight } from '@element-plus/icons-vue'
import type { DemoRun } from '../demo'
import StatusBadge from './StatusBadge.vue'
defineProps<{ runs: DemoRun[] }>()
</script>
<template><div class="table-wrap"><table><thead><tr><th>任务</th><th>类型 / 来源</th><th>状态</th><th>创建时间</th><th>耗时</th><th></th></tr></thead><tbody><tr v-for="run in runs" :key="run.id"><td><router-link :to="`/demo/runs/${run.id}`" class="run-name">{{ run.title }}</router-link><div class="run-id">{{ run.id }}</div></td><td><span class="type-label">{{ run.category }}</span><div class="subline">{{ run.source }}</div></td><td><StatusBadge :status="run.status" /><div v-if="run.delivery === 'unknown'" class="delivery-note">外部动作结果待核对</div></td><td class="muted">{{ run.time }}</td><td class="muted">{{ run.duration }}</td><td><router-link class="row-arrow" :to="`/demo/runs/${run.id}`" :aria-label="`查看${run.title}`"><el-icon><ArrowRight /></el-icon></router-link></td></tr></tbody></table><div v-if="!runs.length" class="empty">没有匹配的示例任务</div></div></template>
