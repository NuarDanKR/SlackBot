import { useEffect, useState } from 'react'
import type { Me } from './api/client'
import { ApiError, login as apiLogin, logout as apiLogout } from './api/client'
import { useResource } from './api/hooks'
import { AuditEvents } from './pages/AuditEvents'
import { ArchiveDiagnostics, AnswerQuality, FeedbackPage, SlackDiagnostics } from './pages/Diagnostics'
import { BatchTimers } from './pages/BatchTimers'
import { Collected } from './pages/Collected'
import { ConsoleUsers } from './pages/ConsoleUsers'
import { Dashboard } from './pages/Dashboard'
import { Deploy } from './pages/Deploy'
import { EnvSettings } from './pages/EnvSettings'
import { Harness } from './pages/Harness'
import { CollectionDashboard, AnswerDashboard, OperationsDashboard, ConsoleDashboard } from './pages/LifecycleDashboards'
import { Questions } from './pages/Questions'
import { ServiceLogs } from './pages/ServiceLogs'
import type { ErrorLogContext } from './pages/ServiceLogs'
import { SpecialistAnalytics, SpecialistManagement } from './pages/Specialists'
import { Usage } from './pages/Usage'
import { Workspaces } from './pages/Workspaces'
import { useHashNavigation, withQuery } from './navigation'
import type { Capabilities, ConsoleRole, ConsoleUser } from './types'

type Theme = 'system' | 'light' | 'dark'
type NavItem = { path: string; label: string; minimum?: ConsoleRole; capability?: keyof Capabilities }
type NavGroup = { label: string; path: string; minimum?: ConsoleRole; items: NavItem[] }

const NAV: NavGroup[] = [
  { label: '수집', path: '/collect', items: [
    { path: '/collect', label: '수집 대시보드' },
    { path: '/collect/status', label: '수집 현황' },
    { path: '/collect/archive', label: '아카이브 진단' },
    { path: '/collect/documents', label: '원문 문서' },
    { path: '/collect/summaries', label: '승인 요약 문서', capability: 'approvedSummaries' },
    { path: '/collect/reviews', label: '요약 검토 현황', capability: 'summaryReview' },
  ] },
  { label: '답변', path: '/answer', items: [
    { path: '/answer', label: '답변 대시보드' },
    { path: '/answer/questions', label: '질문 처리 기록', minimum: 'developer' },
    { path: '/answer/usage', label: '사용량 및 비용' },
    { path: '/answer/specialists', label: '전문 봇 분석', minimum: 'developer', capability: 'specialists' },
    { path: '/answer/quality', label: '답변 품질', minimum: 'developer' },
    { path: '/answer/feedback', label: '피드백', minimum: 'developer' },
    { path: '/answer/rules', label: '답변 규칙', minimum: 'developer' },
  ] },
  { label: '관리', path: '/manage', minimum: 'developer', items: [
    { path: '/manage', label: '운영 대시보드', minimum: 'developer' },
    { path: '/manage/specialists', label: '전문 봇 관리', minimum: 'developer', capability: 'specialists' },
    { path: '/manage/slack', label: 'Slack 연결·명령 진단', minimum: 'developer' },
    { path: '/manage/logs', label: '서비스 로그', minimum: 'developer' },
    { path: '/manage/batches', label: '배치 관리', minimum: 'admin' },
    { path: '/manage/deploy', label: '배포 관리', minimum: 'developer' },
    { path: '/manage/workspaces', label: '워크스페이스 관리', minimum: 'admin' },
    { path: '/manage/environment', label: '환경 설정', minimum: 'admin' },
  ] },
  { label: '콘솔 관리', path: '/console', minimum: 'admin', items: [
    { path: '/console', label: '콘솔 대시보드', minimum: 'admin' },
    { path: '/console/users', label: '콘솔 사용자 관리', minimum: 'admin' },
    { path: '/console/audit', label: '감사 기록', minimum: 'admin' },
  ] },
]

const RANK: Record<ConsoleRole, number> = { guest: 0, developer: 1, admin: 2 }
const THEME_LABEL: Record<Theme, string> = { system: '시스템 설정', light: '밝게', dark: '어둡게' }

function useTheme() {
  const [theme, setTheme] = useState<Theme>(() => (localStorage.getItem('tybot-theme') as Theme) || 'system')
  useEffect(() => {
    if (theme === 'system') document.documentElement.removeAttribute('data-theme')
    else document.documentElement.setAttribute('data-theme', theme)
    localStorage.setItem('tybot-theme', theme)
  }, [theme])
  return { theme, setTheme }
}

function SignIn({ onSignedIn }: { onSignedIn: () => void }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (!email.trim() || !password) return
    setBusy(true); setError(null)
    try { await apiLogin(email.trim(), password); onSignedIn() }
    catch (caught) { setError(caught instanceof ApiError ? caught.message : String(caught)); setPassword('') }
    finally { setBusy(false) }
  }
  return <div className="signin"><div className="signin-card"><span className="brand-mark">TAEYOUNG</span><h1 className="signin-title">태영건설 TYBot 관리 콘솔</h1><p className="signin-note">회사 계정으로 로그인해 주세요.</p>
    {error && <div className="notice bad"><div><div className="notice-title">로그인하지 못했습니다.</div><div className="notice-detail">{error}</div></div></div>}
    <form onSubmit={submit}><div className="field"><label className="field-label" htmlFor="email">회사 이메일</label><input id="email" className="input" type="email" autoComplete="username" autoFocus value={email} onChange={(e) => setEmail(e.target.value)} /></div><div className="field" style={{ marginTop: 14 }}><label className="field-label" htmlFor="password">비밀번호</label><input id="password" className="input" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} /></div><button className="btn btn-primary btn-block" style={{ marginTop: 18 }} disabled={busy}>{busy ? '확인 중' : '로그인'}</button></form>
  </div></div>
}

function roleUser(me: Me): ConsoleUser { return { name: me.name, email: me.email, role: me.role, workspaces: me.workspaces } }

export default function App() {
  const { location, navigate } = useHashNavigation()
  const [authTick, setAuthTick] = useState(0)
  const [toasts, setToasts] = useState<{ id: number; msg: string }[]>([])
  const { theme, setTheme } = useTheme()
  const me = useResource<Me>('/api/me', [authTick])
  const capabilities = useResource<Capabilities>(me.data ? '/api/capabilities' : null)
  function toast(msg: string) { const id = Date.now(); setToasts((rows) => [...rows, { id, msg }]); window.setTimeout(() => setToasts((rows) => rows.filter((row) => row.id !== id)), 4600) }
  async function signOut() { try { await apiLogout() } catch { /* local session still resets */ } setAuthTick((value) => value + 1) }
  if (me.loading) return <div className="signin"><div className="signin-card"><p className="signin-note">계정을 확인하고 있습니다.</p></div></div>
  if (!me.data) return <SignIn onSignedIn={() => setAuthTick((value) => value + 1)} />
  const user = roleUser(me.data)
  const caps = capabilities.data ?? { specialists: false, approvedSummaries: false, summaryReview: false }
  const groups = NAV.filter((group) => RANK[user.role] >= RANK[group.minimum ?? 'guest']).map((group) => ({ ...group, items: group.items.filter((item) => RANK[user.role] >= RANK[item.minimum ?? 'guest'] && (!item.capability || caps[item.capability])) })).filter((group) => group.items.length)
  const allowed = new Set(groups.flatMap((group) => group.items.map((item) => item.path)))
  const path = allowed.has(location.path) ? location.path : '/collect'
  const logContext: ErrorLogContext | null = location.query.get('at') ? { at: location.query.get('at')!, workspace: location.query.get('workspace') ?? '' } : null
  return <div className="shell"><aside className="rail"><div className="brand"><span className="brand-mark">TAEYOUNG</span><div><div className="brand-name">태영건설 TYBot</div><div className="brand-sub">관리 콘솔</div></div></div>
    <nav className="nav" aria-label="주 메뉴">{groups.map((group) => <div key={group.path}><button className={`nav-group nav-group-link ${path === group.path ? 'is-active' : ''}`} onClick={() => navigate(group.path)}>{group.label}</button>{group.items.map((item) => <button key={item.path} className={`nav-item ${path === item.path ? 'is-active' : ''}`} onClick={() => navigate(item.path)} aria-current={path === item.path ? 'page' : undefined}>{item.label}</button>)}</div>)}</nav>
    <div className="rail-foot"><div className="who"><div className="who-avatar">{user.name.slice(0, 1)}</div><div><div className="who-name">{user.name}</div><div className="who-role">{user.role === 'admin' ? '관리자 · 승인 권한' : user.role === 'developer' ? '개발자 · 변경 요청' : '게스트 · 읽기 전용'}</div></div></div><div className="rail-tools"><button className="btn btn-sm btn-quiet" onClick={signOut}>로그아웃</button><button className="btn btn-sm btn-quiet" onClick={() => setTheme(theme === 'dark' ? 'light' : theme === 'light' ? 'system' : 'dark')}>{THEME_LABEL[theme]}</button></div></div>
  </aside><main className="main"><div className="main-inner">
    {path === '/collect' && <CollectionDashboard user={user} navigate={navigate} />}
    {path === '/collect/status' && <Dashboard query={location.query} />}
    {path === '/collect/archive' && <ArchiveDiagnostics />}
    {path === '/collect/documents' && <Collected user={user} query={location.query} onToast={toast} />}
    {path === '/answer' && <AnswerDashboard user={user} navigate={navigate} />}
    {path === '/answer/questions' && <Questions query={location.query} navigate={navigate} />}
    {path === '/answer/usage' && <Usage canViewLogs={user.role !== 'guest'} showRecent={false} onOpenErrorLogs={(context) => navigate(withQuery('/manage/logs', { workspace: context.workspace, at: context.at, level: 'error' }))} />}
    {path === '/answer/specialists' && <SpecialistAnalytics query={location.query} navigate={navigate} />}
    {path === '/answer/quality' && <AnswerQuality />}
    {path === '/answer/feedback' && <FeedbackPage user={user} onToast={toast} />}
    {path === '/answer/rules' && <Harness />}
    {path === '/manage' && <OperationsDashboard user={user} navigate={navigate} />}
    {path === '/manage/specialists' && <SpecialistManagement user={user} query={location.query} onToast={toast} />}
    {path === '/manage/slack' && <SlackDiagnostics />}
    {path === '/manage/logs' && <ServiceLogs context={logContext} />}
    {path === '/manage/batches' && <BatchTimers onToast={toast} />}
    {path === '/manage/deploy' && <Deploy user={user} onToast={toast} />}
    {path === '/manage/workspaces' && <Workspaces selectedKey={location.query.get('workspace')} onToast={toast} />}
    {path === '/manage/environment' && <EnvSettings onToast={toast} />}
    {path === '/console' && <ConsoleDashboard navigate={navigate} />}
    {path === '/console/users' && <ConsoleUsers currentUser={user} onToast={toast} />}
    {path === '/console/audit' && <AuditEvents query={location.query} navigate={navigate} />}
  </div></main><div className="toast-dock" aria-live="polite">{toasts.map((item) => <div className="toast" key={item.id}>{item.msg}</div>)}</div></div>
}
