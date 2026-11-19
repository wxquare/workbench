<script setup lang="ts">
import { computed, provide, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { MagicStick, Grid, List, ChatLineRound, Warning, FolderOpened, Cpu, Connection, Collection, Reading, Timer, DataAnalysis, Plus, Setting, Menu } from '@element-plus/icons-vue'
import { bearerToken, setBearerToken } from './session'
import { chatSidebarKey, emptyChatSidebar } from './chat-sidebar'
const tokenInput = ref(bearerToken.value), identityOpen = ref(!bearerToken.value), mobileOpen = ref(false), sidebarCompact = ref(false)
watch(bearerToken, value => { tokenInput.value = value; if (!value) identityOpen.value = true })
const route = useRoute()
watch(() => route.path, () => { mobileOpen.value = false })
const chatSidebar = reactive(emptyChatSidebar())
provide(chatSidebarKey, chatSidebar)
const home = computed(() => route.path === '/')
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
function applyToken() { setBearerToken(tokenInput.value); if (bearerToken.value) identityOpen.value = false }
</script>
<template>
  <div class="shell" :class="{ 'chat-shell': home, 'mobile-nav-open': mobileOpen, 'sidebar-compact': home && sidebarCompact }">
    <aside id="main-sidebar" class="sidebar">
      <div class="brand-row"><router-link class="brand" to="/" aria-label="我的工作台"><span class="brand-mark"><MagicStick /></span><span><strong>我的工作台</strong><small>MY WORKBENCH</small></span></router-link><button v-if="home" type="button" class="sidebar-toggle" :aria-label="sidebarCompact ? '展开侧栏' : '收起侧栏'" :title="sidebarCompact ? '展开侧栏' : '收起侧栏'" :aria-expanded="!sidebarCompact" aria-controls="main-sidebar" @click="sidebarCompact = !sidebarCompact"><Menu /></button></div>
      <button v-if="home" class="chat-new" type="button" aria-label="新对话" title="新对话" @click="chatSidebar.newThread(); mobileOpen = false"><Plus /><span>新对话</span></button>
      <div v-else class="agent"><span class="agent-icon">A</span><span class="agent-name"><strong>我的工作台</strong><small>智能助手</small></span></div>
      <nav aria-label="主导航"><div class="nav-label">工作台</div><router-link v-for="item in primary" :key="item.path" :to="item.path" :aria-label="item.label" :title="item.label" class="nav-item" :class="{ active: active === item.path }"><component :is="item.icon" class="nav-icon" /><span>{{ item.label }}</span></router-link>
        <section v-if="home" class="sidebar-recents" aria-label="最近会话"><div class="nav-label">最近会话</div><p v-if="!chatSidebar.agent">选择 Agent 后查看</p><p v-else-if="chatSidebar.loading && !chatSidebar.threads.length">加载中…</p><p v-else-if="!chatSidebar.threads.length">暂无会话</p><button v-for="item in chatSidebar.threads" :key="item.thread_id" type="button" class="recent-thread" :class="{ active: chatSidebar.selected === item.thread_id }" :title="item.title || '未命名会话'" @click="chatSidebar.openThread(item); mobileOpen = false">{{ item.title || '未命名会话' }}</button><button v-if="chatSidebar.hasMore" type="button" class="recent-more" :disabled="chatSidebar.loading" @click="chatSidebar.loadMore()">加载更多</button></section>
        <details class="management-nav" open><summary class="nav-label manage-label" aria-label="管理与设置" title="管理与设置"><Setting class="management-icon" /><span>管理与设置</span></summary><router-link v-for="item in manage" :key="item.path" :to="item.path" :aria-label="item.label" :title="item.label" class="nav-item" :class="{ active: active === item.path }"><component :is="item.icon" class="nav-icon" /><span>{{ item.label }}</span></router-link></details>
      </nav>
      <button class="sidebar-bottom identity-toggle" type="button" aria-label="身份凭据" title="身份凭据" :aria-expanded="identityOpen" @click="identityOpen = !identityOpen"><span class="avatar">我</span><span><strong>身份凭据</strong><small>{{ bearerToken ? '已提供' : '未提供' }}</small></span><Setting /></button>
    </aside>
    <div v-if="mobileOpen" class="mobile-nav-backdrop" @click="mobileOpen = false"></div>
    <div class="main-area"><header class="topbar"><button class="mobile-nav-toggle" type="button" aria-label="打开导航" @click="mobileOpen = !mobileOpen"><Menu /></button><div class="breadcrumb">我的工作台 <span>/</span> <strong>{{ active === '/' ? '工作台' : active === '/runs' ? '任务记录' : active === '/threads' ? '会话' : '管理' }}</strong></div><button v-if="home" class="top-new" type="button" @click="chatSidebar.newThread()"><Plus /> 新对话</button></header>
      <main class="page"><form v-if="identityOpen" class="identity-bar" @submit.prevent="applyToken"><label for="bearer">Bearer Token</label><input id="bearer" v-model="tokenInput" type="password" autocomplete="off" placeholder="输入有效的访问令牌" /><button type="submit">使用令牌</button><button v-if="bearerToken" type="button" @click="setBearerToken('')">清除</button><small>仅保存在当前浏览器标签页</small></form><router-view /></main>
    </div>
  </div>
</template>
