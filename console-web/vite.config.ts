import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 콘솔은 VPN(Tailscale/WireGuard) 안에서만 열린다.
// 개발 서버도 같은 전제로 루프백에 묶어 둔다 — 실수로 사내망에 노출되지 않게.
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    // 개발 중에는 /api 요청을 콘솔 API 서버로 넘긴다.
    //   uvicorn tybot.console.app:app --host 127.0.0.1 --port 8787 --app-dir src
    // 운영에서는 같은 프로세스가 화면과 API 를 함께 서빙하므로 프록시가 필요 없다.
    proxy: {
      '/api': { target: 'http://127.0.0.1:8787', changeOrigin: false },
    },
  },
  preview: { host: '127.0.0.1', port: 5173 },
  build: { outDir: 'dist', sourcemap: false },
})
