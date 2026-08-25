import { useEffect, useState } from 'react'
import { ApiError, setToken } from './api/client'
import { useResource } from './api/hooks'
import type { ConsoleUser } from './types'
import { Dashboard } from './pages/Dashboard'
import { Usage } from './pages/Usage'
import { Deploy } from './pages/Deploy'
import { Workspaces } from './pages/Workspaces'
import { Harness } from './pages/Harness'
import { Collected } from './pages/Collected'

type Tab = 'status' | 'collected' | 'usage' | 'harness' | 'deploy' | 'workspaces'

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

/** 서버가 내려주는 사용자 정보. `/api/me` 응답 그대로입니다. */
interface Me {
  name: string
  email: string
  role: 'owner' | 'member'
  workspaces: string[]
  allWorkspaces: boolean
}

function toUser(me: Me): ConsoleUser {
  return { name: me.name, email: me.email, role: me.role, workspaces: me.workspaces }
}

/** 토큰이 없거나 거부됐을 때의 첫 화면. */
function SignIn({ message, onSubmit }: { message?: string; onSubmit: (token: string) => void }) {
  const [value, setValue] = useState('')
  return (
    <div className="signin">
      <div className="signin-card">
        <span className="brand-mark">TAEYOUNG</span>
        <h1 className="signin-title">태영건설 TYBot 관리 콘솔</h1>
        <p className="signin-note">
          관리자에게 받은 접속 토큰을 입력해 주세요. 이 콘솔은 사내 VPN 안에서만 열립니다.
        </p>
        {message && (
          <div className="notice bad" style={{ marginBottom: 14 }}>
            <div>
              <div className="notice-title">접속하지 못했습니다</div>
              <div className="notice-detail">{message}</div>
            </div>
          </div>
        )}
        <form
          onSubmit={(e) => {
            e.preventDefault()
            if (value.trim()) onSubmit(value.trim())
          }}
        >
          <div className="field">
            <label className="field-label" htmlFor="token">
              접속 토큰
            </label>
            <input
              id="token"
              className="input mono"
              type="password"
              autoComplete="off"
              autoFocus
              value={value}
              onChange={(e) => setValue(e.target.value)}
            />
            <span className="field-help">
              토큰은 이 브라우저에만 저장됩니다. 공용 PC 에서는 사용 후 로그아웃해 주세요.
            </span>
          </div>
          <button className="btn btn-primary btn-block" style={{ marginTop: 14 }} type="submit">
            접속
          </button>
        </form>
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

  function signIn(token: string) {
    setToken(token)
    setAuthTick((n) => n + 1)
  }

  function signOut() {
    setToken('')
    setAuthTick((n) => n + 1)
  }

  // 토큰이 없거나 거부됐으면 접속 화면으로 돌아갑니다.
  if (me.error?.needsToken || (!me.loading && !me.data)) {
    return <SignIn message={me.error ? me.error.message : undefined} onSubmit={signIn} />
  }
  if (me.loading || !me.data) {
    return (
      <div className="signin">
        <div className="signin-card">
          <p className="signin-note">접속 중입니다…</p>
        </div>
      </div>
    )
  }
  // 서버는 붙었지만 다른 이유로 실패한 경우(예: 서버 오류)
  if (me.error && !(me.error instanceof ApiError && me.error.needsToken)) {
    return <SignIn message={me.error.message} onSubmit={signIn} />
  }

  const user = toUser(me.data)
  const isOwner = user.role === 'owner'

  const visibleNav = NAV.map((g) => ({
    ...g,
    items: g.items.filter((i) => !i.ownerOnly || isOwner),
  })).filter((g) => g.items.length)

  const activeTab: Tab = !isOwner && tab === 'workspaces' ? 'status' : tab

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

          <span className="rail-note">사내 VPN 안에서만 접속됩니다</span>
        </div>
      </aside>

      <main className="main">
        <div className="main-inner">
          {activeTab === 'status' && <Dashboard user={user} onToast={toast} />}
          {activeTab === 'collected' && <Collected user={user} onToast={toast} />}
          {activeTab === 'usage' && <Usage user={user} onToast={toast} />}
          {activeTab === 'harness' && <Harness user={user} onToast={toast} />}
          {activeTab === 'deploy' && <Deploy user={user} onToast={toast} />}
          {activeTab === 'workspaces' && <Workspaces onToast={toast} />}
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
