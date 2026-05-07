import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// 与 web_server (FastAPI) 共部署：构建产物输出到 dist/，由 FastAPI 在 /dashboard 下挂载。
export default defineConfig({
  plugins: [react()],
  base: '/dashboard/',
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:18888',
      '/ws': { target: 'ws://127.0.0.1:18888', ws: true },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
});
