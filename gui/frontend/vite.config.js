import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// During development, Vite serves the UI on 5173 and proxies /api/*
// to the FastAPI backend on 8765. In production, the React build is
// served by the same FastAPI process from /static, so no proxy needed.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8765',
        changeOrigin: true,
        // SSE needs streaming through the proxy
        ws: false,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    rollupOptions: {
      output: {
        // No code splitting for this app — single-bundle ships
        // faster on a local-network LAN than chunk-loading.
        manualChunks: undefined,
      },
    },
  },
})
