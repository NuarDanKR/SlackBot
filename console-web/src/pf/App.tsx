/** PF 운영 콘솔 (`/pf/`) — 프금팀 Hermes 상태를 보는 읽기 전용 화면.
 *
 * 설계: docs/design/pf-console.md, docs/design/pf-hermes-owner-plan.md §8
 *
 * ## 오늘 여는 것
 * 개요·수집/Git·배치·비용·감사 **조회**뿐입니다. `restart`, `stop`, archive sync,
 * release activate, rollback 버튼은 고정 helper 와 중복 실행 lock, timeout,
 * append-only 감사가 구현된 뒤에만 엽니다. 버튼을 먼저 만들면 「눌러도 아무 일 없음」
 * 과 「눌렀는데 되돌릴 수 없음」 중 하나가 됩니다.
 *
 * ## 화면이 답해야 하는 것
 * 「지금 괜찮은가」 하나가 아닙니다. **아직 안 붙었나 · 못 읽나 · 계약이 덜 지켜졌나 ·
 * 멈췄나 · 괜찮나** 다섯을 구분해서 보여 줍니다. 다섯을 빈 화면 하나로 뭉뚱그리면
 * 사람은 전부 「원래 그런가 보다」 로 읽습니다.
 */
import { useCallback, useEffect, useState } from 'react'
import { Chip, Failed, Loading, Metric, PageHead, Section, fmt } from '../components/primitives'
import type { HealthStatus, PfMe, ServiceHealth, ServiceRef, ServiceRow } from './api'
import { PfApiError, login as pfLogin, logout as pfLogout, me as pfMe, pfApi } from './api'

type Tab = 'overview' | 'archive' | 'batches' | 'cost' | 'audit'

const TABS: { key: Tab; label: string; note: string }[] = [
  { key: 'overview', label: '개요', note: '무엇이 돌고 있고 마지막으로 언제 무엇을 했나' },
  { key: 'archive', label: '수집 · Git', note: 'Slack 수집과 자료 저장소가 따라오고 있나' },
  { key: 'batches', label: '배치', note: '정기 작업이 마지막으로 언제 돌았나' },
  { key: 'cost', label: '비용', note: '오늘 모델을 얼마나 썼나' },
  { key: 'audit', label: '감사', note: '이 화면에서 누가 무엇을 했나' },
]

// ---------------------------------------------------------------------------
// 상태 판정을 사람 말로
// ---------------------------------------------------------------------------

const STATUS_LABEL: Record<HealthStatus, string> = {
  ok: '정상',
  stale: '기록 멈춤',
  incomplete: '계약 미충족',
  unreadable: '읽지 못함',
  unavailable: '상태 없음',
}

const STATUS_TONE: Record<HealthStatus, 'ok' | 'watch' | 'bad' | 'plain'> = {
  ok: 'ok',
  stale: 'bad',
  incomplete: 'watch',
  unreadable: 'bad',
  // 아직 안 붙은 것은 **고장이 아닙니다.** 빨갛게 칠하면 붙이기 전까지 계속
  // 경고가 떠 있고, 사람은 곧 색을 안 보게 됩니다.
  unavailable: 'plain',
}

function StatusChip({ status }: { status: HealthStatus }) {
  return <Chip tone={STATUS_TONE[status]}>{STATUS_LABEL[status]}</Chip>
}

/** 판정과 그 이유를 같이 냅니다. 판정만 보이면 임계값이 틀렸을 때 알 방법이 없습니다. */
function StatusNotice({ health }: { health: ServiceHealth }) {
  if (health.status === 'ok') return null
  const tone = STATUS_TONE[health.status] === 'bad' ? 'bad' : 'warn'
  return (
    <div className={`notice ${tone}`}>
      <div className="notice-kind">{STATUS_LABEL[health.status]}</div>
      <div>
        <div className="notice-title">{health.reason || '상태를 판정할 수 없습니다.'}</div>
        <div className="notice-detail">
          상태 파일: <span className="mono">{health.path}</span>
          {health.fileModifiedAt && <> · 마지막 기록 {fmt.dayClock(health.fileModifiedAt)}</>}
          {health.missing.length > 0 && <> · 빠진 항목 {health.missing.join(', ')}</>}
        </div>
      </div>
    </div>
  )
}

/** 시각 한 칸. 값이 없을 때 빈 칸으로 두지 않습니다 — 빈 칸은 0 으로도, 정상으로도
 *  읽힙니다. 「기록 없음」 이라고 적습니다. */
function When({ value }: { value: unknown }) {
  const text = String(value ?? '').trim()
  if (!text) return <span className="hint">기록 없음</span>
  try {
    return <span>{fmt.dayClock(text)}</span>
  } catch {
    return <span className="mono">{text}</span>
  }
}

const ERROR_LABEL: Record<string, string> = {
  slack: 'Slack 연결',
  anthropic: '모델(Anthropic)',
  git_pull: '자료 Git 받기',
  git_push: '자료 Git 올리기',
  conflict: 'Git 충돌',
  archive_invalid: '아카이브 형식',
  config_invalid: '설정',
  unknown: '분류되지 않음',
}

function ErrorList({ errors }: { errors: { code: string; at: string }[] }) {
  if (!errors.length) return <p className="note">기록된 오류가 없습니다.</p>
  return (
    <div className="table-scroll">
      <table className="table">
        <thead>
          <tr>
            <th>구분</th>
            <th>발생 시각</th>
          </tr>
        </thead>
        <tbody>
          {errors.map((item, index) => (
            <tr key={`${item.code}-${item.at}-${index}`}>
              <td>{ERROR_LABEL[item.code] ?? item.code}</td>
              <td>
                <When value={item.at} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ---------------------------------------------------------------------------
// 화면
// ---------------------------------------------------------------------------

function usePf<T>(path: string | null, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(path !== null)
  const [error, setError] = useState<PfApiError | null>(null)
  const [tick, setTick] = useState(0)
  const reload = useCallback(() => setTick((n) => n + 1), [])
  useEffect(() => {
    if (path === null) {
      setLoading(false)
      return
    }
    let alive = true
    setLoading(true)
    setError(null)
    pfApi
      .get<T>(path)
      .then((got) => alive && setData(got))
      .catch((caught) => alive && setError(caught instanceof PfApiError ? caught : new PfApiError(0, String(caught))))
      .finally(() => alive && setLoading(false))
    return () => {
      alive = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, tick, ...deps])
  return { data, loading, error, reload }
}

function Overview({ service }: { service: string }) {
  const res = usePf<{ service: ServiceRef; health: ServiceHealth }>(`/services/${service}/status`)
  if (res.loading) return <Loading what="서비스 상태를" />
  if (res.error || !res.data)
    return <Failed what="서비스 상태를" detail={res.error?.message ?? '응답이 없습니다.'} onRetry={res.reload} />
  const { service: ref, health } = res.data
  const f = health.fields
  return (
    <>
      <StatusNotice health={health} />
      <Section title="지금 무엇이 돌고 있나">
        <div className="card card-pad">
          <div className="metrics">
            <Metric k="실행 중인 commit" v={String(f.sourceCommit ?? '기록 없음')} />
            <Metric k="운영 상태" v={ref.state === 'active' ? '운영' : ref.state === 'shadow' ? '관찰(shadow)' : '중지'} />
            <Metric k="오늘 모델 호출" v={f.callsToday === undefined ? '기록 없음' : fmt.int(Number(f.callsToday))} />
          </div>
          <div className="table-scroll" style={{ marginTop: 14 }}>
            <table className="table">
              <tbody>
                <tr>
                  <th>시작</th>
                  <td>
                    <When value={f.startedAt} />
                  </td>
                </tr>
                <tr>
                  <th>상태 기록</th>
                  <td>
                    <When value={f.generatedAt} />
                  </td>
                </tr>
                <tr>
                  <th>마지막 Slack 연결</th>
                  <td>
                    <When value={f.lastSlackConnect} />
                  </td>
                </tr>
                <tr>
                  <th>마지막 수집</th>
                  <td>
                    <When value={f.lastIngest} />
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </Section>

      <Section title="소유자" lead="정책 변경과 서비스 중지 승인은 업무 소유자가, 계정·시크릿·복구는 인프라 소유자가 맡습니다.">
        <div className="card card-pad">
          <div className="metrics">
            <Metric k="업무 소유자" v={ref.businessOwner || '미지정'} />
            <Metric k="인프라 소유자" v={ref.infraOwner || '미지정'} />
          </div>
        </div>
      </Section>

      <Section title="기록된 오류" lead="장애를 종류별로 구분합니다. 어느 계층이 끊겼는지 알아야 어디를 볼지 정할 수 있습니다.">
        <div className="card card-pad">
          <ErrorList errors={health.errors} />
        </div>
      </Section>

      <Section title="이 화면에서 할 수 없는 것">
        <div className="card card-pad">
          <p className="note">
            재시작·중지·자료 Git 동기화·릴리스 활성화·롤백은 <strong>아직 열지 않았습니다.</strong>
            고정 helper, 중복 실행 lock, timeout, 되돌릴 수 있는 감사가 준비된 뒤에 엽니다.
            그때까지 이 작업들은 승인된 수동 runbook 으로 서버에서 합니다.
          </p>
        </div>
      </Section>
    </>
  )
}

interface ArchiveData {
  service: ServiceRef
  status: HealthStatus
  reason: string
  lastIngest: string
  lastSlackConnect: string
  lastArchivePull: string
  lastArchivePush: string
  unpushedCommits: number | null
  archiveConflict: string
  errors: { code: string; at: string }[]
}

function Archive({ service }: { service: string }) {
  const res = usePf<ArchiveData>(`/services/${service}/archive`)
  if (res.loading) return <Loading what="수집·Git 상태를" />
  if (res.error || !res.data)
    return <Failed what="수집·Git 상태를" detail={res.error?.message ?? '응답이 없습니다.'} onRetry={res.reload} />
  const d = res.data
  const unpushed = d.unpushedCommits
  return (
    <>
      {d.status !== 'ok' && (
        <div className="notice warn">
          <div className="notice-kind">{STATUS_LABEL[d.status]}</div>
          <div>
            <div className="notice-title">{d.reason || '상태를 판정할 수 없습니다.'}</div>
            <div className="notice-detail">아래 값은 마지막으로 기록된 것이며 지금 상태가 아닐 수 있습니다.</div>
          </div>
        </div>
      )}

      <Section title="Slack 수집">
        <div className="card card-pad">
          <div className="table-scroll">
            <table className="table">
              <tbody>
                <tr>
                  <th>마지막 Slack 연결</th>
                  <td>
                    <When value={d.lastSlackConnect} />
                  </td>
                </tr>
                <tr>
                  <th>마지막 수집</th>
                  <td>
                    <When value={d.lastIngest} />
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </Section>

      <Section
        title="자료 저장소 (Git)"
        lead="이 화면은 Git 을 읽기만 합니다. merge·push·force push 는 콘솔에 없습니다 — 되돌릴 수 없는 조작을 버튼 하나 뒤에 두지 않습니다."
      >
        <div className="card card-pad">
          <div className="metrics">
            <Metric k="올리지 못한 commit" v={unpushed === null || unpushed === undefined ? '기록 없음' : fmt.int(unpushed)} unit={unpushed ? '개' : ''} />
          </div>
          <div className="table-scroll" style={{ marginTop: 14 }}>
            <table className="table">
              <tbody>
                <tr>
                  <th>마지막 받기(pull)</th>
                  <td>
                    <When value={d.lastArchivePull} />
                  </td>
                </tr>
                <tr>
                  <th>마지막 올리기(push)</th>
                  <td>
                    <When value={d.lastArchivePush} />
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          {d.archiveConflict ? (
            <div className="notice bad" style={{ marginTop: 14 }}>
              <div className="notice-kind">충돌</div>
              <div>
                <div className="notice-title">자료 저장소에 충돌이 있습니다</div>
                <div className="notice-detail">
                  <span className="mono">{d.archiveConflict}</span> — 서버에서 사람이 풉니다. 콘솔은 자동 merge 를 하지 않습니다.
                </div>
              </div>
            </div>
          ) : (
            <p className="note">충돌 기록이 없습니다.</p>
          )}
        </div>
      </Section>

      <Section title="수집·Git 오류">
        <div className="card card-pad">
          <ErrorList errors={d.errors} />
        </div>
      </Section>
    </>
  )
}

interface BatchData {
  service: ServiceRef
  status: HealthStatus
  reason: string
  jobs: { key: string; label: string; lastRunAt: string }[]
}

function Batches({ service }: { service: string }) {
  const res = usePf<BatchData>(`/services/${service}/batches`)
  if (res.loading) return <Loading what="배치 상태를" />
  if (res.error || !res.data)
    return <Failed what="배치 상태를" detail={res.error?.message ?? '응답이 없습니다.'} onRetry={res.reload} />
  const d = res.data
  return (
    <Section
      title="정기 작업"
      lead="마지막으로 언제 돌았는지만 보여 줍니다. 이 콘솔은 다른 서비스의 systemd 를 만지지 않습니다 — 만질 수 있으면 조회 화면이 아닙니다."
    >
      <div className="card card-pad">
        {d.status !== 'ok' && <p className="note">{d.reason}</p>}
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th>작업</th>
                <th>마지막 실행</th>
              </tr>
            </thead>
            <tbody>
              {d.jobs.map((job) => (
                <tr key={job.key}>
                  <td>{job.label}</td>
                  <td>
                    <When value={job.lastRunAt} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </Section>
  )
}

interface CostData {
  service: ServiceRef
  status: HealthStatus
  reason: string
  callsToday: number | null
  tokensToday: number | null
  costUsdToday: number | null
  lastModelCall: string
  errors: { code: string; at: string }[]
  limitOwner: string
}

function Cost({ service }: { service: string }) {
  const res = usePf<CostData>(`/services/${service}/cost`)
  if (res.loading) return <Loading what="비용을" />
  if (res.error || !res.data)
    return <Failed what="비용을" detail={res.error?.message ?? '응답이 없습니다.'} onRetry={res.reload} />
  const d = res.data
  return (
    <>
      <Section title="오늘 사용량" lead="서비스가 스스로 기록한 값입니다. 청구서가 아니라 관찰값입니다.">
        <div className="card card-pad">
          {d.status !== 'ok' && <p className="note">{d.reason}</p>}
          <div className="metrics">
            <Metric k="호출" v={d.callsToday === null || d.callsToday === undefined ? '기록 없음' : fmt.int(d.callsToday)} unit={d.callsToday ? '건' : ''} />
            <Metric k="토큰" v={d.tokensToday === null || d.tokensToday === undefined ? '기록 없음' : fmt.int(d.tokensToday)} />
            <Metric k="비용" v={d.costUsdToday === null || d.costUsdToday === undefined ? '기록 없음' : fmt.usd(d.costUsdToday)} />
          </div>
          <div className="table-scroll" style={{ marginTop: 14 }}>
            <table className="table">
              <tbody>
                <tr>
                  <th>마지막 모델 호출</th>
                  <td>
                    <When value={d.lastModelCall} />
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </Section>

      <Section title="상한은 누가 거나">
        <div className="card card-pad">
          <p className="note">
            모델 계정과 일일 상한은 <strong>{d.limitOwner}</strong>이 가지고 있습니다. 이 화면에서는 상한을
            걸 수 없고, 걸 수 있다고 착각하면 초과했을 때 아무도 막지 않게 됩니다. 여기서는 보기만 하고,
            이상하면 업무 소유자에게 알립니다.
          </p>
        </div>
      </Section>

      <Section title="모델 오류">
        <div className="card card-pad">
          <ErrorList errors={d.errors} />
        </div>
      </Section>
    </>
  )
}

interface AuditRow {
  at: string
  actor: string
  service: string
  action: string
  outcome: string
  detail: string
}

function Audit() {
  const res = usePf<{ events: AuditRow[] }>('/audit?limit=200')
  if (res.loading) return <Loading what="감사 기록을" />
  if (res.error || !res.data)
    return <Failed what="감사 기록을" detail={res.error?.message ?? '응답이 없습니다.'} onRetry={res.reload} />
  const rows = res.data.events
  return (
    <Section
      title="감사 기록"
      note={`${rows.length}건`}
      lead="이 콘솔에서 일어난 일만 남습니다. 질문·답변·문서 본문과 시크릿은 담지 않습니다."
    >
      <div className="card card-pad">
        {rows.length === 0 ? (
          <p className="note">아직 기록이 없습니다.</p>
        ) : (
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr>
                  <th>시각</th>
                  <th>사용자</th>
                  <th>서비스</th>
                  <th>작업</th>
                  <th>결과</th>
                  <th>내용</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row, index) => (
                  <tr key={`${row.at}-${index}`}>
                    <td>
                      <When value={row.at} />
                    </td>
                    <td>{row.actor}</td>
                    <td>{row.service || '-'}</td>
                    <td className="mono">{row.action}</td>
                    <td>{row.outcome === 'succeeded' ? '성공' : row.outcome === 'denied' ? '거부' : '실패'}</td>
                    <td>{row.detail || '-'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </Section>
  )
}

// ---------------------------------------------------------------------------
// 로그인
// ---------------------------------------------------------------------------

function SignIn({ onSignedIn }: { onSignedIn: () => void }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (!email.trim() || !password) return
    setBusy(true)
    setError(null)
    try {
      await pfLogin(email.trim(), password)
      onSignedIn()
    } catch (caught) {
      setError(caught instanceof PfApiError ? caught.message : String(caught))
      setPassword('')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="signin">
      <div className="signin-card">
        <span className="brand-mark">TAEYOUNG</span>
        <h1 className="signin-title">PF 운영 콘솔</h1>
        <p className="signin-note">회사 계정으로 로그인해 주세요.</p>
        {error && (
          <div className="notice bad">
            <div>
              <div className="notice-title">로그인하지 못했습니다.</div>
              <div className="notice-detail">{error}</div>
            </div>
          </div>
        )}
        <form onSubmit={submit}>
          <div className="field">
            <label className="field-label" htmlFor="pf-email">
              회사 이메일
            </label>
            <input
              id="pf-email"
              className="input"
              type="email"
              autoComplete="username"
              autoFocus
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />
          </div>
          <div className="field" style={{ marginTop: 14 }}>
            <label className="field-label" htmlFor="pf-password">
              비밀번호
            </label>
            <input
              id="pf-password"
              className="input"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </div>
          <button className="btn btn-primary btn-block" style={{ marginTop: 18 }} disabled={busy}>
            {busy ? '확인 중' : '로그인'}
          </button>
        </form>
      </div>
    </div>
  )
}

/** 로그인은 됐는데 볼 것이 없는 상태. **빈 화면으로 두지 않습니다** — 빈 화면은
 *  고장으로 읽히고, 사람은 로그인을 다시 시도합니다. */
function NoAccess({ user, onSignOut }: { user: PfMe; onSignOut: () => void }) {
  return (
    <div className="signin">
      <div className="signin-card">
        <span className="brand-mark">TAEYOUNG</span>
        <h1 className="signin-title">볼 수 있는 서비스가 없습니다</h1>
        <p className="signin-note">
          {user.email} 로 로그인했지만 이 콘솔에서 볼 수 있는 서비스가 없습니다. TYBot 관리 콘솔의 권한과
          이 콘솔의 권한은 별개입니다 — 인프라 소유자에게 PF 서비스 권한을 요청하세요.
        </p>
        <button className="btn btn-quiet btn-block" style={{ marginTop: 18 }} onClick={onSignOut}>
          로그아웃
        </button>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// 껍데기
// ---------------------------------------------------------------------------

export default function PfApp() {
  const [user, setUser] = useState<PfMe | null>(null)
  const [checked, setChecked] = useState(false)
  const [tab, setTab] = useState<Tab>('overview')
  const [service, setService] = useState('')

  const refresh = useCallback(() => {
    pfMe()
      .then((got) => {
        setUser(got)
        setService((current) => current || got.services[0]?.key || '')
      })
      .catch(() => setUser(null))
      .finally(() => setChecked(true))
  }, [])

  useEffect(refresh, [refresh])

  const rows = usePf<{ services: ServiceRow[] }>(user && user.services.length ? '/services' : null, [user])

  if (!checked) return <Loading what="PF 콘솔을" />
  if (!user) return <SignIn onSignedIn={refresh} />

  async function signOut() {
    await pfLogout().catch(() => undefined)
    setUser(null)
  }

  if (!user.services.length) return <NoAccess user={user} onSignOut={signOut} />

  const current = rows.data?.services.find((row) => row.key === service) ?? null
  const tabMeta = TABS.find((item) => item.key === tab)!

  return (
    <div className="shell">
      <header className="topbar">
        <div className="topbar-left">
          <span className="brand-mark">TAEYOUNG</span>
          <span className="topbar-title">PF 운영 콘솔</span>
          {current && <StatusChip status={current.health.status} />}
        </div>
        <div className="topbar-right">
          <span className="hint">{user.name}</span>
          <button className="btn btn-sm btn-quiet" onClick={signOut}>
            로그아웃
          </button>
        </div>
      </header>

      <main className="content">
        <PageHead
          crumb={`PF · ${current?.label ?? service}`}
          title={tabMeta.label}
          note={tabMeta.note}
          aside={
            user.services.length > 1 ? (
              <select className="input" value={service} onChange={(event) => setService(event.target.value)}>
                {user.services.map((item) => (
                  <option key={item.key} value={item.key}>
                    {rows.data?.services.find((row) => row.key === item.key)?.label ?? item.key}
                  </option>
                ))}
              </select>
            ) : current ? (
              <Chip tone="plain">{current.roles.join(' · ') || '권한 없음'}</Chip>
            ) : null
          }
        />

        <nav className="tabs">
          {TABS.map((item) => (
            <button
              key={item.key}
              type="button"
              className={`tab${tab === item.key ? ' is-active' : ''}`}
              onClick={() => setTab(item.key)}
            >
              {item.label}
            </button>
          ))}
        </nav>

        {tab === 'overview' && <Overview service={service} />}
        {tab === 'archive' && <Archive service={service} />}
        {tab === 'batches' && <Batches service={service} />}
        {tab === 'cost' && <Cost service={service} />}
        {tab === 'audit' && <Audit />}
      </main>
    </div>
  )
}
