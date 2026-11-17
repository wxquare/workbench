<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { FolderOpened } from '@element-plus/icons-vue'
const route = useRoute()
const labels: Record<string, [string, string]> = {
  agents: ['Agent', '管理团队的模型、仓库、Skill 和工具配置。'], threads: ['会话', '查看多轮会话和关联任务。'],
  models: ['模型配置', '管理模型端点、参数及发布版本。'], skills: ['Skills', '查看技能资源、版本和发布状态。'],
  tools: ['工具连接', '管理工具配置、发现和调用状态。'], schedules: ['定时任务', '查看巡检计划和执行历史。'],
  incidents: ['事件中心', '查看事件结论、证据和跟进情况。'], statistics: ['数据统计', '了解任务、费用、反馈和知识缺口。'],
}
const page = computed(() => labels[String(route.params.section)] ?? ['管理', ''])
</script>
<template><div class="page-heading"><div><div class="eyebrow">AGENT <span></span> 管理</div><h1>{{ page[0] }}</h1><p>{{ page[1] }}</p></div></div><section class="panel placeholder"><span class="placeholder-icon"><el-icon><FolderOpened /></el-icon></span><h2>{{ page[0] }}</h2><p>该页面的真实数据和操作将在 M3 接口完成后接入。</p><router-link to="/" class="text-link">返回工作台 →</router-link></section></template>
