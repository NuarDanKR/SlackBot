import { existsSync } from 'node:fs'
import { spawn } from 'node:child_process'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const consoleDir = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const repoDir = resolve(consoleDir, '..')
const venvPython =
  process.platform === 'win32'
    ? resolve(repoDir, '.venv', 'Scripts', 'python.exe')
    : resolve(repoDir, '.venv', 'bin', 'python')
const python = existsSync(venvPython) ? venvPython : process.platform === 'win32' ? 'python' : 'python3'

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
