/** PF 콘솔 API 호출.
 *
 * 서버는 `src/tybot_pf/app.py` 입니다. TYBot 콘솔(`src/api/client.ts`)과 **다른
 * 프로세스**이므로 클라이언트도 따로 둡니다. 같은 모듈을 쓰면 기본 경로 하나를
 * 잘못 고쳐 PF 화면이 TYBot API 를 부르게 되고, 그건 화면에서 정상으로 보입니다.
 *
 * 세션은 `pf_console` 쿠키입니다(`Path=/pf`). 브라우저가 `/pf/` 요청에만 붙이므로
 * TYBot 콘솔로 가는 요청에는 실려 가지 않습니다.
 */

const BASE = '/pf/api'

export class PfApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message)
    this.name = 'PfApiError'
  }

  get needsLogin(): boolean {
    return this.status === 401
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(`${BASE}${path}`, {
      ...init,
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    })
  } catch (e) {
    throw new PfApiError(0, `PF 콘솔 서버가 응답하지 않습니다. (원인: ${e})`)
  }
  if (!res.ok) {
    let detail = ''
    try {
      detail = ((await res.json()) as { detail?: string }).detail ?? ''
    } catch {
      detail = ''
    }
    throw new PfApiError(res.status, detail || `요청이 실패했습니다 (${res.status}).`)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

export const pfApi = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
}

// --- 서버가 내려주는 모양 ---------------------------------------------------

/** 상태 판정. 「빈 화면」 하나로 뭉뚱그리지 않습니다 — 아직 안 붙은 것과 어제 죽은
 *  것을 구분하지 못하면 사람은 둘 다 「원래 그런가 보다」 로 읽습니다. */
export type HealthStatus = 'ok' | 'stale' | 'incomplete' | 'unreadable' | 'unavailable'

export interface ServiceHealth {
  service: string
  status: HealthStatus
  reason: string
  path: string
  fields: Record<string, string | number>
  errors: { code: string; at: string }[]
  missing: string[]
  fileModifiedAt: string
  ageSeconds: number | null
}

export interface ServiceRef {
  key: string
  label: string
  state: 'active' | 'shadow' | 'disabled'
  businessOwner: string
  infraOwner: string
}

export interface ServiceRow extends ServiceRef {
  roles: string[]
  health: ServiceHealth
}

export interface PfMe {
  email: string
  name: string
  services: { key: string; roles: string[] }[]
}

export async function login(email: string, password: string): Promise<PfMe> {
  const got = await pfApi.post<{ user: PfMe }>('/login', { email, password })
  return got.user
}

export async function logout(): Promise<void> {
  await pfApi.post('/logout')
}

export async function me(): Promise<PfMe> {
  const got = await pfApi.get<{ user: PfMe }>('/me')
  return got.user
}
