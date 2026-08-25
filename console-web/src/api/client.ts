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
    // 서버가 꺼져 있거나 VPN 이 끊긴 경우입니다. 원인을 사람 말로 바꿔 줍니다.
    throw new ApiError(
      0,
      `서버에 연결하지 못했습니다. VPN 연결과 콘솔 서버 상태를 확인해 주세요. (${e})`,
    )
  }

  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`
    try {
      const body = await res.json()
      if (body?.detail) detail = String(body.detail)
    } catch {
      // 본문이 JSON 이 아니면 상태 코드만 씁니다.
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
