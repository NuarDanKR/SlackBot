import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

/**
 * PF 운영 콘솔(`/pf/`) 빌드.
 *
 * TYBot 콘솔과 **같은 소스 트리에서 공통 컴포넌트를 재사용하되 산출물은 따로** 만든다.
 * 한 번들에 둘을 넣으면 프로세스를 나눈 의미가 절반 사라진다 — PF 화면 코드가
 * TYBot API 클라이언트를 들고 있게 되고, 경로 하나를 잘못 고치면 조용히 넘어간다.
 *
 * `root` 를 `pf/` 로 두는 이유: 그래야 산출물이 `dist-pf/index.html` 로 나온다.
 * 루트에 두 번째 html 을 두면 이름이 `pf.html` 로 남아 정적 서빙이 index 를 못 찾는다.
 */
export default defineConfig({
  root: 'pf',
  base: '/pf/',
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5174,
    // 개발 중에는 /pf/api 요청을 PF 콘솔 API 서버로 넘긴다.
    //   uvicorn tybot_pf.app:app --host 127.0.0.1 --port 8788 --app-dir src
    proxy: {
      '/pf/api': { target: 'http://127.0.0.1:8788', changeOrigin: false },
    },
    fs: {
      // 공통 컴포넌트가 `pf/` 밖(`src/`)에 있다.
      allow: ['..'],
    },
  },
  build: { outDir: '../dist-pf', emptyOutDir: true, sourcemap: false },
})
