import { useEffect, useState } from 'react'
import type { Me } from './api/client'
import { ApiError, login as apiLogin, logout as apiLogout } from './api/client'
import { useResource } from './api/hooks'
import type { ConsoleUser } from './types'
import { Dashboard } from './pages/Dashboard'
import { Usage } from './pages/Usage'
import { Deploy } from './pages/Deploy'
import { Workspaces } from './pages/Workspaces'
import { Harness } from './pages/Harness'
import { Collected } from './pages/Collected'
import { EnvSettings } from './pages/EnvSettings'

type Tab = 'status' | 'collected' | 'usage' | 'harness' | 'deploy' | 'workspaces' | 'env'

/** 메뉴 이름은 화면 제목과 똑같이 씁니다 — 어디에 있는지 헷갈리지 않게 합니다. */
const NAV: { group: string; items: { id: Tab; label: string; ownerOnly?: boolean }[] }[] = [
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
      { id: 'harness', label: '봇 규칙 편집' },
      { id: 'deploy', label: '배포 승인' },
      { id: 'workspaces', label: '워크스페이스 관리', ownerOnly: true },
      { id: 'env', label: '환경변수 설정', ownerOnly: true },
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

/** 로그인 화면. 아이디·비밀번호를 받아 세션 쿠키를 발급받습니다. */
function SignIn({ onSignedIn }: { onSignedIn: () => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    if (!username.trim() || !password) return
    setBusy(true)
    setError(null)
    try {
      await apiLogin(username.trim(), password)
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
            <label className="field-label" htmlFor="username">
              아이디
            </label>
            <input
              id="username"
              className="input"
              autoComplete="username"
              autoFocus
              value={username}
              onChange={(e) => setUsername(e.target.value)}
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
  const isOwner = user.role === 'owner'

  const visibleNav = NAV.map((g) => ({
    ...g,
    items: g.items.filter((i) => !i.ownerOnly || isOwner),
  })).filter((g) => g.items.length)

  const activeTab: Tab = !isOwner && (tab === 'workspaces' || tab === 'env') ? 'status' : tab

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
                {isOwner ? '관리자 · 승인 권한' : '담당자 · 요청 권한'}
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
          {me.data.usingDefaultAccount && (
            <div className="notice bad" style={{ marginBottom: 20 }}>
              <div className="notice-kind">임시 계정</div>
              <div>
                <div className="notice-title">기본 계정으로 접속 중입니다</div>
                <div className="notice-detail">
                  이 콘솔은 봇 토큰과 배포 권한을 다루는 화면입니다. 콘솔에 접속할 수 있는 누구나
                  같은 계정으로 들어올 수 있으니, 운영 전에 계정을 교체해 주세요.
                </div>
              </div>
            </div>
          )}
          {activeTab === 'status' && <Dashboard user={user} onToast={toast} />}
          {activeTab === 'collected' && <Collected user={user} onToast={toast} />}
          {activeTab === 'usage' && <Usage user={user} onToast={toast} />}
          {activeTab === 'harness' && <Harness user={user} onToast={toast} />}
          {activeTab === 'deploy' && <Deploy user={user} onToast={toast} />}
          {activeTab === 'workspaces' && <Workspaces onToast={toast} />}
          {activeTab === 'env' && <EnvSettings onToast={toast} />}
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
