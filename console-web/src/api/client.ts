/** 콘솔 API 호출.
 *
 * 서버는 `src/tybot/console/app.py` 입니다. 개발 중에는 Vite 가 `/api` 를 그쪽으로 넘깁니다
 * (`vite.config.ts` 의 proxy). 운영에서는 같은 프로세스가 화면과 API 를 함께 서빙하므로
 * 상대 경로 그대로 동작합니다.
 *
 * ## 로그인
 * 회사 이메일·비밀번호로 `POST /api/login` 하면 서버가 **HttpOnly 세션 쿠키**를 내려줍니다.
 * 이후 요청은 브라우저가 그 쿠키를 자동으로 붙입니다.
 *
 * 화면 코드가 세션 값을 들고 있지 않습니다. localStorage 에 토큰을 두면 화면에서 실행되는
 * 어떤 스크립트든 그 값을 읽을 수 있지만, HttpOnly 쿠키는 스크립트가 읽지 못합니다.
 */

/**
 * 서버에 닿지 못했을 때의 안내.
 *
 * ## 왜 명령을 안 적나
 * 전에는 `uvicorn ...` 한 줄을 적어 두었다. 그건 **개발 PC 기준**인데, 이 화면은
 * 운영 서버에서도 뜬다. 2026-09-22 에 운영 콘솔이 안 열렸을 때 사람이 그 줄을 그대로
 * 서버에 붙여 넣었고 `bash: uvicorn: command not found` 만 봤다 — 서버에는 `uvicorn`
 * 이 PATH 에 없고 venv 절대경로라야 한다(CLAUDE.md 「서버 명령은 가상환경 기준으로」).
 *
 * 틀린 명령은 안내가 아니라 **한 단계 더 헤매게 만드는 것**이다. 그래서 화면은
 * 명령을 주지 않고, 어디를 봐야 하는지만 말한다. 두 환경에서 원인이 다르므로
 * 둘을 갈라 적는다.
 */
const SERVER_DOWN = [
  'API 서버에 닿지 못했습니다.',
  '',
  '· 개발 PC: 저장소 루트에서 `npm run dev:all` 로 화면과 API 를 함께 띄웁니다.',
  '· 운영 서버: 서비스가 떠 있는지와 앞단 프록시를 확인합니다.',
  '    sudo systemctl status tybot-console --no-pager',
  '    sudo nginx -t && sudo systemctl status nginx --no-pager',
  '  콘솔은 루프백에만 바인딩하므로 nginx 가 없으면 브라우저에서 닿지 않습니다(B-35).',
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
      headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
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
  securePost: <T>(path: string, body?: unknown) =>
    request<T>(path, {
      method: 'POST',
      body: JSON.stringify(body ?? {}),
      headers: { 'X-TYBot-CSRF': '1' },
    }),
  secureUpload: <T>(path: string, file: File) =>
    request<T>(path, {
      method: 'POST',
      body: file,
      headers: {
        'Content-Type': 'application/zip',
        'X-TYBot-CSRF': '1',
        'X-TYBot-Filename': encodeURIComponent(file.name),
      },
    }),
  put: <T>(path: string, body: unknown) =>
    request<T>(path, {
      method: 'PUT',
      body: JSON.stringify(body),
      headers: { 'X-TYBot-CSRF': '1' },
    }),
}

/** Slack 앱 매니페스트 — 저장소 파일을 서버가 그대로 읽어 줍니다.
 *
 * 화면에 상수로 박아 두면 스코프가 늘 때마다 두 곳을 고쳐야 하고, 한쪽만 고치면
 * 이 화면을 보고 만든 앱에 권한이 빠져 봇이 오류 없이 반쪽만 동작합니다.
 */
export interface Manifest {
  content: string
  path: string
  updatedAt: string
  sha256: string
}

export async function fetchManifest(): Promise<Manifest> {
  const raw = await api.get<{
    content: string
    path: string
    updated_at: string
    sha256: string
  }>('/api/manifest')
  return {
    content: raw.content,
    path: raw.path,
    updatedAt: raw.updated_at,
    sha256: raw.sha256,
  }
}

export interface Me {
  name: string
  email: string
  role: 'guest' | 'developer' | 'admin'
  workspaces: string[]
  allWorkspaces: boolean
}

export function login(email: string, password: string): Promise<Me> {
  return api.post<Me>('/api/login', { email, password })
}

export function logout(): Promise<{ ok: boolean }> {
  return api.post<{ ok: boolean }>('/api/logout')
}
