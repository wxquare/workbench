import { createRouter, createWebHistory } from 'vue-router'
import Workbench from './views/Workbench.vue'
import Runs from './views/Runs.vue'
import RunDetail from './views/RunDetail.vue'
import DemoRunDetail from './views/DemoRunDetail.vue'
import Agents from './views/Agents.vue'
import Graphs from './views/Graphs.vue'
import Models from './views/Models.vue'
import Threads from './views/Threads.vue'
import Skills from './views/Skills.vue'
import Knowledge from './views/Knowledge.vue'
import KnowledgeEntry from './views/KnowledgeEntry.vue'
import Placeholder from './views/Placeholder.vue'

export default createRouter({
  history: createWebHistory('/app/'),
  routes: [
    { path: '/', name: 'home', component: Workbench },
    { path: '/runs', name: 'runs', component: Runs },
    { path: '/runs/:id', name: 'run-detail', component: RunDetail },
    { path: '/demo/runs/:id', name: 'demo-run-detail', component: DemoRunDetail },
    { path: '/agents', name: 'agents', component: Agents },
    { path: '/graphs', name: 'graphs', component: Graphs },
    { path: '/graphs/:id', name: 'graph-detail', component: Graphs },
    { path: '/models', name: 'models', component: Models },
    { path: '/threads', name: 'threads', component: Threads },
    { path: '/skills', name: 'skills', component: Skills },
    { path: '/knowledge', name: 'knowledge', component: Knowledge },
    { path: '/knowledge/:id/versions/:version', name: 'knowledge-version', component: KnowledgeEntry },
    { path: '/:section(tools|schedules|incidents|statistics)', name: 'section', component: Placeholder },
  ],
})
