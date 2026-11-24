import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'
export default defineConfig({
  base: '/app/',
  plugins: [vue()],
  server: { proxy: { '/v1': 'http://127.0.0.1:8000' } },
  test: { environment: 'jsdom' },
})
