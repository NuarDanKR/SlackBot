/** 콘솔 API 호출.
 *
 * 서버는 `src/tybot/console/app.py` 입니다. 개발 중에는 Vite 가 `/api` 를 그쪽으로 넘깁니다
 * (`vite.config.ts` 의 proxy). 운영에서는 같은 프로세스가 화면과 API 를 함께 서빙하므로
 * 상대 경로 그대로 동작합니다.
 *
 * ## 접속 토큰
 * 서버가 `Authorization: Bearer <토큰>` 으로 사용자를 구분합니다(`CONSOLE_USERS`).
 * 토큰은 브라우저의 localStorage 에 둡니다. 콘솔은 사내 VPN 안에서만 열리고, 토큰이 없으면
 * 첫 화면에서 입력을 받습니다. 로그아웃은 저장된 값을 지우는 것입니다.
 */

const TOKEN_KEY = 'tybot-console-token'

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? ''
}

export function setToken(token: string): void {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }

  /** 토큰이 없거나 틀렸다 — 화면은 토큰 입력으로 돌아가야 합니다. */
  get needsToken(): boolean {
    return this.status === 401
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(path, {
      ...init,
      headers: {
        ...(init?.headers ?? {}),
        Authorization: `Bearer ${getToken()}`,
        'Content-Type': 'application/json',
      },
    })
  } catch (e) {
    // 서버가 꺼져 있거나 VPN 이 끊긴 경우입니다. 원인을 사람 말로 바꿔 줍니다.
    throw new ApiError(0, `서버에 연결하지 못했습니다. VPN 연결과 콘솔 서버 상태를 확인해 주세요. (${e})`)
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
  post: <T>(path: string, body: unknown) =>
    request<T>(path, { method: 'POST', body: JSON.stringify(body) }),
}
