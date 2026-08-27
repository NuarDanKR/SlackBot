import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    // 개발 중에는 /api 요청을 콘솔 API 서버로 넘긴다.
    //   npm run api   (또는 npm run dev:all 로 화면과 함께)
    // API 서버가 꺼져 있으면 이 프록시는 빈 500 을 돌려준다. 그 경우의 안내 문구는
    // src/api/client.ts 에서 만든다(사유 없는 5xx = 서버 미기동으로 본다).
    // 운영에서는 같은 프로세스가 화면과 API 를 함께 서빙하므로 프록시가 필요 없다.
    proxy: {
      '/api': { target: 'http://127.0.0.1:8787', changeOrigin: false },
    },
  },
  preview: { host: '127.0.0.1', port: 5173 },
  build: { outDir: 'dist', sourcemap: false },
})
