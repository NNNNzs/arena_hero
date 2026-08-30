import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  base: '/static/app/',
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8787',
      '/static': {
        target: 'http://127.0.0.1:8787',
        bypass: (req) => {
          const pathname = req.url?.split('?')[0] ?? ''
          if (pathname === '/static/app' || pathname.startsWith('/static/app/')) {
            return req.url
          }
          return undefined
        },
      },
    },
  },
  build: {
    outDir: '../arena_tactic/web/static/app',
    emptyOutDir: true,
    copyPublicDir: false,
    sourcemap: false,
  },
})
