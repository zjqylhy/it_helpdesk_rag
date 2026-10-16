import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', 'HD_')
  return { plugins: [vue()], server: { proxy: { '/api': { target: env.HD_API_TARGET ?? 'http://127.0.0.1:18080' } } } }
})
