import { createApp } from 'vue'
import { ElButton, ElInput, ElOption, ElSelect } from 'element-plus'
import 'element-plus/dist/index.css'
import App from './App.vue'
import router from './router'
import './style.css'
import './qa.css'

createApp(App).use(router).use(ElButton).use(ElInput).use(ElOption).use(ElSelect).mount('#app')

import "./chat.css"
