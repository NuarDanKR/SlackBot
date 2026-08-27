/** 콘솔 API 호출.
 *
 * 서버는 `src/tybot/console/app.py` 입니다. 개발 중에는 Vite 가 `/api` 를 그쪽으로 넘깁니다
 * (`vite.config.ts` 의 proxy). 운영에서는 같은 프로세스가 화면과 API 를 함께 서빙하므로
 * 상대 경로 그대로 동작합니다.
 *
 * ## 로그인
 * 아이디·비밀번호로 `POST /api/login` 하면 서버가 **HttpOnly 세션 쿠키**를 내려줍니다.
 * 이후 요청은 브라우저가 그 쿠키를 자동으로 붙입니다.
 *
 * 화면 코드가 세션 값을 들고 있지 않습니다. localStorage 에 토큰을 두면 화면에서 실행되는
 * 어떤 스크립트든 그 값을 읽을 수 있지만, HttpOnly 쿠키는 스크립트가 읽지 못합니다.
 */

const SERVER_DOWN = [
  'API 서버가 응답하지 않습니다. 저장소 루트에서 아래 명령으로 서버를 먼저 띄워 주세요.',
  'uvicorn tybot.console.app:app --host 127.0.0.1 --port 8787 --app-dir src',
].join('\n')

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }

  /** 로그인이 필요하거나 만료됐다 — 화면은 로그인으로 돌아가야 합니다. */
  get needsLogin(): boolean {
    return this.status === 401
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(path, {
      ...init,
      // 세션 쿠키를 함께 보냅니다.
      credentials: 'same-origin',
      headers: { ...(init?.headers ?? {}), 'Content-Type': 'application/json' },
    })
  } catch (e) {
    // 서버가 꺼져 있거나 네트워크가 끊긴 경우입니다. 원인을 사람 말로 바꿔 줍니다.
    throw new ApiError(0, `${SERVER_DOWN} (원인: ${e})`)
  }

  if (!res.ok) {
    let detail = ''
    try {
      const body = await res.json()
      if (body?.detail) detail = String(body.detail)
    } catch {
      // 본문이 JSON 이 아닌 경우입니다(아래에서 상태 코드로 사유를 만듭니다).
    }
    if (!detail) {
      // 서버가 사유를 주지 못한 5xx 는 대개 "서버가 안 떠 있음"입니다.
      // 그대로 "500 Internal Server Error" 라고 보여 주면 무엇을 해야 할지 알 수 없습니다.
      detail =
        res.status >= 500
          ? SERVER_DOWN
          : `요청이 거절되었습니다. (${res.status} ${res.statusText})`
    }
    throw new ApiError(res.status, detail)
  }
  return (await res.json()) as T
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: JSON.stringify(body ?? {}) }),
}

export interface Me {
  name: string
  email: string
  role: 'owner' | 'member'
  workspaces: string[]
  allWorkspaces: boolean
  /** 임시 계정(admin/1111)으로 열려 있으면 true — 화면에 경고를 띄웁니다. */
  usingDefaultAccount: boolean
}

export function login(username: string, password: string): Promise<Me> {
  return api.post<Me>('/api/login', { username, password })
}

export function logout(): Promise<{ ok: boolean }> {
  return api.post<{ ok: boolean }>('/api/logout')
}
