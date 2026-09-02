import { useEffect, useState } from 'react'
import type { Me } from './api/client'
import { ApiError, login as apiLogin, logout as apiLogout } from './api/client'
import { useResource } from './api/hooks'
import type { ConsoleUser } from './types'
import { Dashboard } from './pages/Dashboard'
import { Usage } from './pages/Usage'
import { Harness } from './pages/Harness'
import { Collected } from './pages/Collected'
import { HealthCheck } from './pages/HealthCheck'
import { EnvSettings } from './pages/EnvSettings'
import { ConsoleUsers } from './pages/ConsoleUsers'
import { ServiceLogs } from './pages/ServiceLogs'
import type { ConsoleRole } from './types'

type Tab =
  | 'status'
  | 'collected'
  | 'usage'
  | 'health'
  | 'harness'
  | 'env'
  | 'users'
  | 'logs'

/** 메뉴 이름은 화면 제목과 똑같이 씁니다 — 어디에 있는지 헷갈리지 않게 합니다. */
const NAV: { group: string; items: { id: Tab; label: string; minimum?: ConsoleRole }[] }[] = [
  {
    group: '데이터',
    items: [
      { id: 'status', label: '데이터 현황' },
      { id: 'collected', label: '수집 문서 열람' },
      { id: 'usage', label: 'API 사용량' },
    ],
  },
  {
    group: '봇 관리',
    items: [
      { id: 'health', label: '헬스 체크', minimum: 'developer' },
      { id: 'logs', label: '서비스 로그', minimum: 'developer' },
      { id: 'harness', label: '봇 규칙 열람', minimum: 'developer' },
      { id: 'env', label: '환경변수 설정', minimum: 'admin' },
      { id: 'users', label: '콘솔 사용자 관리', minimum: 'admin' },
    ],
  },
]

type Theme = 'system' | 'light' | 'dark'
const THEME_LABEL: Record<Theme, string> = { system: '시스템 설정', light: '밝게', dark: '어둡게' }

function useTheme() {
  const [theme, setTheme] = useState<Theme>(
    () => (localStorage.getItem('tybot-theme') as Theme) || 'system',
  )
  useEffect(() => {
    const root = document.documentElement
    if (theme === 'system') root.removeAttribute('data-theme')
    else root.setAttribute('data-theme', theme)
    localStorage.setItem('tybot-theme', theme)
  }, [theme])
  return { theme, setTheme }
}

function toUser(me: Me): ConsoleUser {
  return { name: me.name, email: me.email, role: me.role, workspaces: me.workspaces }
}

/** 로그인 화면. 회사 이메일·비밀번호를 받아 세션 쿠키를 발급받습니다. */
function SignIn({ onSignedIn }: { onSignedIn: () => void }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    if (!email.trim() || !password) return
    setBusy(true)
    setError(null)
    try {
      await apiLogin(email.trim(), password)
      onSignedIn()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err))
      setPassword('')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="signin">
      <div className="signin-card">
        <span className="brand-mark">TAEYOUNG</span>
        <h1 className="signin-title">태영건설 TYBot 관리 콘솔</h1>
        <p className="signin-note">사내 계정으로 로그인해 주세요.</p>

        {error && (
          <div className="notice bad" style={{ marginBottom: 14 }}>
            <div>
              <div className="notice-title">로그인하지 못했습니다</div>
              <div className="notice-detail">{error}</div>
            </div>
          </div>
        )}

        <form onSubmit={submit}>
          <div className="field">
            <label className="field-label" htmlFor="email">
              회사 이메일
            </label>
            <input
              id="email"
              className="input"
              type="email"
              autoComplete="username"
              inputMode="email"
              autoFocus
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </div>
          <div className="field" style={{ marginTop: 14 }}>
            <label className="field-label" htmlFor="password">
              비밀번호
            </label>
            <input
              id="password"
              className="input"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
          <button
            className="btn btn-primary btn-block"
            style={{ marginTop: 18 }}
            type="submit"
            disabled={busy}
          >
            {busy ? '확인 중…' : '로그인'}
          </button>
        </form>

        <p className="signin-foot">
          비밀번호를 잊었거나 계정이 없으면 관리자에게 문의해 주세요.
        </p>
      </div>
    </div>
  )
}

export default function App() {
  const [tab, setTab] = useState<Tab>('status')
  const [toasts, setToasts] = useState<{ id: number; msg: string }[]>([])
  const { theme, setTheme } = useTheme()
  const [authTick, setAuthTick] = useState(0)

  const me = useResource<Me>('/api/me', [authTick])

  function toast(msg: string) {
    const id = Date.now() + Math.floor(performance.now())
    setToasts((t) => [...t, { id, msg }])
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 4600)
  }

  async function signOut() {
    try {
      await apiLogout()
    } catch {
      // 서버에 못 닿아도 화면은 로그인으로 돌려보냅니다.
    }
    setAuthTick((n) => n + 1)
  }

  // 아직 확인 중일 때는 빈 화면 대신 안내를 둡니다.
  if (me.loading) {
    return (
      <div className="signin">
        <div className="signin-card">
          <p className="signin-note">확인 중입니다…</p>
        </div>
      </div>
    )
  }
  // 로그인이 필요하거나 만료됐으면 로그인 화면으로 돌아갑니다.
  if (!me.data) {
    return <SignIn onSignedIn={() => setAuthTick((n) => n + 1)} />
  }

  const user = toUser(me.data)
  const rank: Record<ConsoleRole, number> = { guest: 0, developer: 1, admin: 2 }

  const visibleNav = NAV.map((g) => ({
    ...g,
    items: g.items.filter((i) => rank[user.role] >= rank[i.minimum ?? 'guest']),
  })).filter((g) => g.items.length)

  const allowedTabs = new Set(visibleNav.flatMap((group) => group.items.map((item) => item.id)))
  const activeTab: Tab = allowedTabs.has(tab) ? tab : 'status'

  return (
    <div className="shell">
      <aside className="rail">
        <div className="brand">
          <span className="brand-mark">TAEYOUNG</span>
          <div>
            <div className="brand-name">태영건설 TYBot</div>
            <div className="brand-sub">관리 콘솔</div>
          </div>
        </div>

        <nav className="nav" aria-label="주 메뉴">
          {visibleNav.map((g) => (
            <div key={g.group}>
              <div className="nav-group">{g.group}</div>
              {g.items.map((t) => (
                <button
                  key={t.id}
                  className={`nav-item ${activeTab === t.id ? 'is-active' : ''}`}
                  onClick={() => setTab(t.id)}
                  aria-current={activeTab === t.id ? 'page' : undefined}
                >
                  <span>{t.label}</span>
                </button>
              ))}
            </div>
          ))}
        </nav>

        <div className="rail-foot">
          <div className="who">
            <div className="who-avatar" aria-hidden="true">
              {user.name.slice(0, 1)}
            </div>
            <div>
              <div className="who-name">{user.name}</div>
              <div className="who-role">
                {user.role === 'admin'
                  ? '관리자 · 승인 권한'
                  : user.role === 'developer'
                    ? '개발자 · 변경 요청'
                    : '게스트 · 읽기 전용'}
              </div>
            </div>
          </div>

          <div className="rail-tools">
            <button className="btn btn-sm btn-quiet" onClick={signOut}>
              로그아웃
            </button>
            <button
              className="btn btn-sm btn-quiet"
              onClick={() =>
                setTheme(theme === 'dark' ? 'light' : theme === 'light' ? 'system' : 'dark')
              }
            >
              {THEME_LABEL[theme]}
            </button>
          </div>

        </div>
      </aside>

      <main className="main">
        <div className="main-inner">
          {activeTab === 'status' && <Dashboard />}
          {activeTab === 'collected' && <Collected user={user} onToast={toast} />}
          {activeTab === 'usage' && <Usage />}
          {activeTab === 'health' && <HealthCheck user={user} />}
          {activeTab === 'logs' && <ServiceLogs />}
          {activeTab === 'harness' && <Harness />}
          {activeTab === 'env' && <EnvSettings onToast={toast} />}
          {activeTab === 'users' && <ConsoleUsers currentUser={user} onToast={toast} />}
        </div>
      </main>

      <div className="toast-dock" aria-live="polite">
        {toasts.map((t) => (
          <div className="toast" key={t.id}>
            {t.msg}
          </div>
        ))}
      </div>
    </div>
  )
}
