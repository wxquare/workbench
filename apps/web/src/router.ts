import { createRouter, createWebHistory } from 'vue-router'
import Workbench from './views/Workbench.vue'
import Runs from './views/Runs.vue'
import RunDetail from './views/RunDetail.vue'
import DemoRunDetail from './views/DemoRunDetail.vue'
import Agents from './views/Agents.vue'
import Models from './views/Models.vue'
import Threads from './views/Threads.vue'
import Skills from './views/Skills.vue'
import Placeholder from './views/Placeholder.vue'

export default createRouter({
  history: createWebHistory('/app/'),
  routes: [
    { path: '/', name: 'home', component: Workbench },
    { path: '/runs', name: 'runs', component: Runs },
    { path: '/runs/:id', name: 'run-detail', component: RunDetail },
    { path: '/demo/runs/:id', name: 'demo-run-detail', component: DemoRunDetail },
    { path: '/agents', name: 'agents', component: Agents },
    { path: '/models', name: 'models', component: Models },
    { path: '/threads', name: 'threads', component: Threads },
    { path: '/skills', name: 'skills', component: Skills },
    { path: '/:section(tools|schedules|incidents|statistics)', name: 'section', component: Placeholder },
  ],
})
