// 로컬 개발용 TYBot 콘솔 API(uvicorn) 실행기.
//
// console-web 은 SlackBot 과 **별도 저장소**다. API 서버 코드(`src/tybot/console/`)와
// 파이썬 가상환경(`.venv`)은 SlackBot 저장소에 있으므로 그 위치를 찾아야 한다.
//
// 찾는 순서:
//   1. TYBOT_ROOT 환경변수          (예: set TYBOT_ROOT=C:\dev\SlackBot)
//   2. console-web 의 상위 폴더      (분리 전 구조: SlackBot/console-web)
//   3. console-web 옆의 SlackBot     (분리 후 권장 구조: dev/SlackBot, dev/console-web)
import { existsSync } from 'node:fs'
import { spawn } from 'node:child_process'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const consoleDir = resolve(dirname(fileURLToPath(import.meta.url)), '..')

const isTybotRoot = (dir) => existsSync(resolve(dir, 'src', 'tybot', 'console', 'app.py'))

const candidates = [
  process.env.TYBOT_ROOT,
  resolve(consoleDir, '..'),
  resolve(consoleDir, '..', 'SlackBot'),
].filter(Boolean)

const repoDir = candidates.map((dir) => resolve(dir)).find(isTybotRoot)

if (!repoDir) {
  console.error('TYBot(SlackBot) 저장소를 찾지 못했습니다. 확인한 경로:')
  for (const dir of candidates) console.error(`  - ${resolve(dir)}`)
  console.error('TYBOT_ROOT 환경변수로 SlackBot 경로를 지정하세요.')
  console.error('  PowerShell:  $env:TYBOT_ROOT = "C:\\dev\\SlackBot"; npm run api')
  process.exit(1)
}

const venvPython =
  process.platform === 'win32'
    ? resolve(repoDir, '.venv', 'Scripts', 'python.exe')
    : resolve(repoDir, '.venv', 'bin', 'python')
const python = existsSync(venvPython) ? venvPython : process.platform === 'win32' ? 'python' : 'python3'

console.log(`API 서버: ${repoDir}`)

const child = spawn(
  python,
  [
    '-m',
    'uvicorn',
    'tybot.console.app:app',
    '--host',
    '127.0.0.1',
    '--port',
    '8787',
    '--app-dir',
    'src',
    '--reload',
  ],
  { cwd: repoDir, stdio: 'inherit' },
)

child.on('error', (error) => {
  console.error(`API 서버를 시작하지 못했습니다: ${error.message}`)
  process.exitCode = 1
})

child.on('exit', (code, signal) => {
  process.exitCode = code ?? (signal === 'SIGINT' ? 130 : 1)
})

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => child.kill(signal))
}
