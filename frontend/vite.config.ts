import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

const rootDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, rootDir, '')
  const backendHost = env.AUTO_TEAM_BACKEND_HOST || '127.0.0.1'
  const backendPort = env.AUTO_TEAM_BACKEND_PORT || '18087'
  const proxyTarget = (env.VITE_API_BASE_URL || `http://${backendHost}:${backendPort}`).replace(/\/$/, '')

  return {
    plugins: [react(), tailwindcss()],
    server: {
      host: true,
      port: Number(env.AUTO_TEAM_FRONTEND_PORT || '5173'),
      proxy: {
        '/api': {
          target: proxyTarget,
          changeOrigin: true,
        },
      },
    },
  }
})
