<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { Grid, List, ChatLineRound, FolderOpened, Cpu, Collection, Reading, Connection, Timer, Warning, DataAnalysis, MagicStick } from '@element-plus/icons-vue'
import { bearerToken, setBearerToken } from './session'
const tokenInput = ref(bearerToken.value)
watch(bearerToken, value => { tokenInput.value = value })
const route = useRoute()
const primary = [
  { label: '工作台', path: '/', icon: Grid }, { label: '任务记录', path: '/runs', icon: List },
  { label: '会话', path: '/threads', icon: ChatLineRound }, { label: '事件中心', path: '/incidents', icon: Warning },
]
const manage = [
  { label: 'Agent 管理', path: '/agents', icon: FolderOpened }, { label: '模型配置', path: '/models', icon: Cpu },
  { label: '工作流', path: '/graphs', icon: Connection }, { label: 'Skills', path: '/skills', icon: Collection }, { label: '知识库', path: '/knowledge', icon: Reading }, { label: '工具连接', path: '/tools', icon: Connection },
  { label: '定时任务', path: '/schedules', icon: Timer }, { label: '数据统计', path: '/statistics', icon: DataAnalysis },
]
const active = computed(() => route.path.startsWith('/runs') ? '/runs' : route.path.startsWith('/knowledge') ? '/knowledge' : route.path.startsWith('/graphs') ? '/graphs' : route.path)
</script>
<template>
  <div class="shell">
    <aside class="sidebar">
      <router-link class="brand" to="/" aria-label="我的工作台"><span class="brand-mark"><el-icon><MagicStick /></el-icon></span><span><strong>我的工作台</strong><small>MY WORKBENCH</small></span></router-link>
      <div class="agent"><span class="agent-icon">A</span><span class="agent-name"><strong>我的工作台</strong><small>智能助手</small></span></div>
      <nav aria-label="主导航"><div class="nav-label">工作台</div><router-link v-for="item in primary" :key="item.path" :to="item.path" :aria-label="item.label" :title="item.label" class="nav-item" :class="{ active: active === item.path }"><el-icon><component :is="item.icon" /></el-icon><span>{{ item.label }}</span></router-link>
        <div class="nav-label manage-label">管理</div><router-link v-for="item in manage" :key="item.path" :to="item.path" :aria-label="item.label" :title="item.label" class="nav-item" :class="{ active: active === item.path }"><el-icon><component :is="item.icon" /></el-icon><span>{{ item.label }}</span></router-link>
      </nav>
      <div class="sidebar-bottom"><span class="avatar">我</span><span><strong>身份凭据</strong><small>{{ bearerToken ? '已提供' : '未提供' }}</small></span></div>
    </aside>
    <div class="main-area"><header class="topbar"><div class="breadcrumb">我的工作台 <span>/</span> <strong>{{ active === '/' ? '工作台' : active === '/runs' ? '任务记录' : active === '/threads' ? '会话' : '管理' }}</strong></div></header>
      <main class="page"><form class="identity-bar" @submit.prevent="setBearerToken(tokenInput)"><label for="bearer">Bearer Token</label><input id="bearer" v-model="tokenInput" type="password" autocomplete="off" placeholder="输入有效的访问令牌" /><button type="submit">使用令牌</button><button v-if="bearerToken" type="button" @click="setBearerToken('')">清除</button><small>仅保存在当前浏览器标签页</small></form><router-view /></main>
    </div>
  </div>
</template>
