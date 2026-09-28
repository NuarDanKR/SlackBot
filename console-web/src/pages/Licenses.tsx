import { useEffect, useMemo, useState } from 'react'
import { ApiError, api } from '../api/client'
import { useResource } from '../api/hooks'
import { Chip, Failed, Loading, PageHead, Section, fmt } from '../components/primitives'
import './Licenses.css'

/**
 * 라이선스 현황 — 할당은 사람이 입력하고, 활성은 Slack 이 말합니다.
 *
 * 서버: `src/tybot/console/license_store.py`. 표에는 Slack 에 연동된 워크스페이스만
 * 나옵니다. 활성 수는 Slack `users.list` 로 세고, 할당은 Slack 에서 가져올 수 없어
 * 사람이 적습니다.
 */

interface LicenseRow {
  workspace: string
  label: string
  allocated: number
  /** Slack 조회에 실패하면 null 입니다 — 0 이 아니라 「모름」 입니다. */
  active: number | null
  guests: number
  fetchedAt: string | null
  error: string | null
}

interface LicenseResponse {
  rows: LicenseRow[]
  linkedCount: number
  syncedCount: number
  syncedAt: string | null
  cacheSeconds: number
  result?: { saved: number }
}

type Scope = 'all' | 'over'

function toNumber(value: string): number {
  const n = Number(value)
  return Number.isFinite(n) ? n : 0
}

function isCount(value: string): boolean {
  return /^\d+$/.test(value.trim())
}

/** 활성화율 배지. 할당보다 많으면 빨간 「초과」 입니다. */
function RateBadge({ allocated, active }: { allocated: number; active: number | null }) {
  if (active === null) return <span className="license-rate plain">조회 실패</span>
  if (allocated <= 0) return <span className="license-rate plain">—</span>
  if (active > allocated) return <span className="license-rate bad">초과 {fmt.int(active - allocated)}</span>
  const rate = Math.round((active / allocated) * 100)
  const tone = rate >= 90 ? 'ok' : rate >= 50 ? 'warn' : 'bad'
  return <span className={`license-rate ${tone}`}>{rate}%</span>
}

function LinkMark({ error }: { error: string | null }) {
  return <span className="license-link" title={error ?? 'Slack 연동 · 활성 수를 자동으로 가져옵니다'}>
    <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" focusable="false">
      <path d="M6.6 9.4a2.6 2.6 0 0 0 3.7 0l2.4-2.4a2.6 2.6 0 0 0-3.7-3.7l-.9.9M9.4 6.6a2.6 2.6 0 0 0-3.7 0L3.3 9a2.6 2.6 0 0 0 3.7 3.7l.9-.9"
        fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
  </span>
}

export function Licenses({ onToast }: { onToast: (message: string) => void }) {
  const [refreshTick, setRefreshTick] = useState(0)
  const resource = useResource<LicenseResponse>(
    refreshTick ? `/api/licenses?refresh=true&t=${refreshTick}` : '/api/licenses',
  )
  const [report, setReport] = useState<LicenseResponse | null>(null)
  /** 할당 입력칸. 워크스페이스 키 → 입력 문자열. 저장할 때 숫자로 바꿉니다. */
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [search, setSearch] = useState('')
  const [scope, setScope] = useState<Scope>('all')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  function apply(data: LicenseResponse) {
    setReport(data)
    setDrafts(Object.fromEntries(data.rows.map((row) => [row.workspace, String(row.allocated)])))
  }

  useEffect(() => {
    if (resource.data) apply(resource.data)
  }, [resource.data])

  const rows = useMemo(() => (report?.rows ?? []).map((row) => {
    const text = drafts[row.workspace] ?? String(row.allocated)
    return { ...row, text, allocatedNow: toNumber(text) }
  }), [report, drafts])

  const changed = rows.filter((row) => row.text.trim() !== String(row.allocated))
  const invalid = rows.filter((row) => !isCount(row.text))

  const visible = useMemo(() => {
    const q = search.trim().toLowerCase()
    return rows.filter((row) => {
      if (q && !row.label.toLowerCase().includes(q) && !row.workspace.includes(q)) return false
      if (scope === 'over') return row.active !== null && row.active > row.allocatedNow
      return true
    })
  }, [rows, search, scope])

  const bars = useMemo(() => rows
    .map((row) => ({ row, rate: row.allocatedNow > 0 && row.active !== null ? row.active / row.allocatedNow : null }))
    .sort((a, b) => (b.rate ?? -1) - (a.rate ?? -1) || a.row.label.localeCompare(b.row.label, 'ko')), [rows])

  const totals = useMemo(() => ({
    allocated: rows.reduce((sum, row) => sum + row.allocatedNow, 0),
    active: rows.reduce((sum, row) => sum + (row.active ?? 0), 0),
  }), [rows])

  if (resource.loading && !report) return <Loading what="라이선스 현황을" />
  if (resource.error && !report) {
    return <Failed what="라이선스 현황을" detail={resource.error.message} onRetry={resource.reload} />
  }
  if (!report) return null

  const dirty = changed.length > 0
  const failed = rows.filter((row) => row.error)

  async function save() {
    if (saving || !dirty || invalid.length) return
    setSaving(true)
    setError(null)
    try {
      const result = await api.put<LicenseResponse>('/api/licenses', {
        rows: changed.map((row) => ({ workspace: row.workspace, allocated: toNumber(row.text) })),
      })
      apply(result)
      onToast(`할당 라이선스 ${changed.length}건을 저장했습니다.`)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught))
    } finally {
      setSaving(false)
    }
  }

  const syncChip = report.linkedCount
    ? failed.length
      ? <Chip tone="watch">Slack 연동 · {failed.length}개 조회 실패</Chip>
      : <Chip tone="ok">Slack 연동됨 · {report.syncedCount}개 워크스페이스 자동 동기화</Chip>
    : <Chip tone="plain">Slack 연동 워크스페이스 없음</Chip>

  return (
    <>
      <PageHead
        crumb="운영 · 라이선스"
        title="라이선스 현황"
        note="Slack 에 연동된 워크스페이스별로 할당 라이선스와 실제 활성 수를 비교합니다."
        aside={<button className="btn btn-sm" disabled={resource.loading || dirty}
          title={dirty
            ? '저장하지 않은 변경이 있습니다. 먼저 저장해 주세요 — 다시 조회하면 입력값이 서버 값으로 돌아갑니다.'
            : `Slack 조회 결과는 ${Math.round(report.cacheSeconds / 60)}분 동안 재사용합니다.`}
          onClick={() => setRefreshTick((value) => value + 1)}>
          {resource.loading ? 'Slack 조회 중…' : 'Slack 다시 조회'}
        </button>}
      />

      {error && (
        <div className="notice bad">
          <div className="notice-kind">저장 실패</div>
          <div><div className="notice-title">할당 라이선스를 저장하지 못했습니다</div>
            <div className="notice-detail">{error}</div></div>
        </div>
      )}

      <Section title="워크스페이스별 라이선스 사용 현황 (할당 대비 활성)" aside={syncChip}>
        <div className="card card-pad">
          {bars.length ? <div className="license-bars">
            {bars.map(({ row, rate }) => (
              <div className="license-bar-row" key={row.workspace}>
                <span className="license-bar-label">{row.label}<LinkMark error={row.error} /></span>
                <span className="license-bar-track" aria-hidden="true">
                  <span className={`license-bar-fill ${rate !== null && rate >= 1 ? 'bad' : 'ok'}`}
                    style={{ width: `${rate === null ? 0 : Math.min(100, rate * 100)}%` }} />
                </span>
                <span className="license-bar-value">
                  {row.active === null ? '조회 실패' : `${fmt.int(row.active)}개`} / {fmt.int(row.allocatedNow)}개
                  {rate !== null && ` (${Math.round(rate * 100)}%)`}
                </span>
              </div>
            ))}
          </div> : <p className="license-note">Slack 에 연동된 워크스페이스가 없습니다. 운영 &gt; 워크스페이스 관리에서 먼저 등록해 주세요.</p>}
          {report.syncedAt && <p className="license-note">Slack 기준 시각 {fmt.dayClock(report.syncedAt)} · 활성은 비활성화되지 않은 사람 계정 수입니다(봇·단일 채널 게스트 제외). Slack 공정 청구로 빠지는 미접속 인원은 반영되지 않아 청구 수량보다 클 수 있습니다.</p>}
        </div>
      </Section>

      <Section
        title="워크스페이스별 상세 현황"
        lead={'할당 라이선스는 Slack 에서 가져올 수 없어 직접 입력합니다. 입력 후 "저장" 버튼을 눌러 반영하세요. 활성 라이선스는 Slack 에서 실제 값으로 자동 채워지고, 할당보다 활성이 많으면 빨간 배지로 표시됩니다.'}
        aside={<div className="license-tools">
          <input className="input" type="search" placeholder="워크스페이스 검색" aria-label="워크스페이스 검색"
            value={search} onChange={(event) => setSearch(event.target.value)} />
          <select className="input" aria-label="워크스페이스 범위" value={scope}
            onChange={(event) => setScope(event.target.value as Scope)}>
            <option value="all">전체 워크스페이스</option>
            <option value="over">할당 초과</option>
          </select>
          <button className="btn btn-primary" disabled={!dirty || saving || invalid.length > 0} onClick={() => { void save() }}>
            {saving ? '저장 중…' : '저장'}
          </button>
        </div>}
      >
        {(dirty || invalid.length > 0) && <p className="license-dirty">
          {invalid.length > 0 ? '할당 라이선스는 0 이상의 정수로 입력해 주세요.' : `저장하지 않은 변경이 ${changed.length}건 있습니다.`}
        </p>}
        <div className="card"><div className="table-wrap"><table className="table license-table">
          <thead><tr>
            <th>워크스페이스명</th><th className="num">할당(개)</th><th className="num">활성(개)</th>
            <th className="license-center">활성화율 / 초과</th>
          </tr></thead>
          <tbody>
            {visible.map((row) => <tr key={row.workspace}>
              <td>{row.label}
                {row.guests > 0 && <div className="hint">다중 채널 게스트 {fmt.int(row.guests)}명 포함</div>}
                {row.error && <div className="hint warn">{row.error}</div>}
              </td>
              <td className="num"><input className="input license-input" type="number" min="0" step="1" inputMode="numeric"
                aria-label={`${row.label} 할당 라이선스`} value={row.text}
                onChange={(event) => setDrafts((current) => ({ ...current, [row.workspace]: event.target.value }))} /></td>
              <td className="num">{row.active === null ? <span className="hint">—</span> : fmt.int(row.active)}</td>
              <td className="license-center"><span className="license-rate-cell">
                <RateBadge allocated={row.allocatedNow} active={row.active} /><LinkMark error={row.error} />
              </span></td>
            </tr>)}
            {!visible.length && <tr><td colSpan={4}>{rows.length ? '조건에 맞는 워크스페이스가 없습니다.' : 'Slack 에 연동된 워크스페이스가 없습니다.'}</td></tr>}
          </tbody>
          <tfoot><tr className="license-total">
            <td>전체 합계</td>
            <td className="num">{fmt.int(totals.allocated)}</td>
            <td className="num">{fmt.int(totals.active)}</td>
            <td className="license-center"><RateBadge allocated={totals.allocated} active={totals.active} /></td>
          </tr></tfoot>
        </table></div></div>
      </Section>
    </>
  )
}
