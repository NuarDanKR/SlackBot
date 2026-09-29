import { useEffect, useMemo, useState } from 'react'
import { ApiError, api } from '../api/client'
import { useResource } from '../api/hooks'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { Chip, Failed, Loading, PageHead, Section, fmt } from '../components/primitives'
import './Licenses.css'

/**
 * 라이선스 현황 — 할당은 사람이 입력하고, 활성은 Slack 이 말합니다.
 *
 * 서버: `src/tybot/console/license_store.py`.
 * - Slack 연동 워크스페이스: 활성은 Slack `users.list` 로 세고, 할당만 사람이 적습니다.
 * - 직접 추가한 워크스페이스: 아직 연동되지 않은 곳이라 할당·활성을 둘 다 적습니다.
 *   **삭제할 수 있는 것은 이쪽뿐입니다.** 연동 워크스페이스는 레지스트리에서 오므로
 *   지워도 다음 조회에 다시 나타납니다.
 */

type Kind = 'slack' | 'manual'

interface LicenseRow {
  kind: Kind
  /** 직접 추가한 워크스페이스의 번호. 연동 워크스페이스는 null 입니다. */
  id: number | null
  /** 연동 워크스페이스 키. 직접 추가한 곳은 null 입니다. */
  workspace: string | null
  label: string
  allocated: number
  /** 연동 워크스페이스에서 Slack 조회에 실패하면 null 입니다 — 0 이 아니라 「모름」 입니다. */
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
  result?: { saved: number; added: number; updated: number; deleted: number }
}

/** 화면에서 고치는 한 줄. 입력칸은 문자열로 들고 저장할 때 숫자로 바꿉니다. */
interface Draft {
  uid: string
  kind: Kind
  id: number | null
  workspace: string | null
  label: string
  allocated: string
  /** 직접 추가한 워크스페이스만 씁니다. 연동 워크스페이스는 `slackActive` 입니다. */
  active: string
  slackActive: number | null
  guests: number
  error: string | null
  /** 서버에서 읽은 값. 바뀐 줄만 저장합니다. */
  saved: { label: string; allocated: string; active: string } | null
}

type Scope = 'all' | 'slack' | 'manual' | 'over'

function toDraft(row: LicenseRow): Draft {
  const allocated = String(row.allocated)
  const active = row.kind === 'manual' ? String(row.active ?? 0) : ''
  return {
    uid: row.kind === 'slack' ? `ws:${row.workspace}` : `id:${row.id}`,
    kind: row.kind,
    id: row.id,
    workspace: row.workspace,
    label: row.label,
    allocated,
    active,
    slackActive: row.kind === 'slack' ? row.active : null,
    guests: row.guests,
    error: row.error,
    saved: { label: row.label, allocated, active },
  }
}

function toNumber(value: string): number {
  const n = Number(value)
  return Number.isFinite(n) ? n : 0
}

function isCount(value: string): boolean {
  return /^\d+$/.test(value.trim())
}

function nameKey(label: string): string {
  return label.replace(/\s+/g, '').toLowerCase()
}

function activeOf(row: Draft): number | null {
  return row.kind === 'slack' ? row.slackActive : toNumber(row.active)
}

function isChanged(row: Draft): boolean {
  if (!row.saved) return true
  if (row.allocated.trim() !== row.saved.allocated) return true
  return row.kind === 'manual' && (row.label.trim() !== row.saved.label || row.active.trim() !== row.saved.active)
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

function KindMark({ row }: { row: Draft }) {
  if (row.kind === 'manual') {
    return <span className="license-link manual" title="Slack 에 연동되지 않아 직접 추가한 워크스페이스입니다. 할당·활성을 직접 입력합니다.">직접 추가</span>
  }
  return <span className="license-link" title={row.error ?? 'Slack 연동 · 활성 수를 자동으로 가져옵니다'}>
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
  const [rows, setRows] = useState<Draft[]>([])
  /** 저장하면 지워질, 이미 저장돼 있던 직접 추가 워크스페이스. */
  const [removed, setRemoved] = useState<{ id: number; label: string }[]>([])
  const [newSeq, setNewSeq] = useState(0)
  const [search, setSearch] = useState('')
  const [scope, setScope] = useState<Scope>('all')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [confirmSave, setConfirmSave] = useState(false)

  function apply(data: LicenseResponse) {
    setReport(data)
    setRows(data.rows.map(toDraft))
    setRemoved([])
  }

  useEffect(() => {
    if (resource.data) apply(resource.data)
  }, [resource.data])

  const changed = rows.filter(isChanged)

  /** 줄마다 무엇이 틀렸는지. 저장 버튼은 하나라도 있으면 막습니다. */
  const problems = useMemo(() => {
    const out = new Map<string, string>()
    const names = new Map<string, number>()
    for (const row of rows) names.set(nameKey(row.label), (names.get(nameKey(row.label)) ?? 0) + 1)
    for (const row of rows) {
      if (!isCount(row.allocated)) out.set(row.uid, '할당은 0 이상의 정수로 입력해 주세요.')
      else if (row.kind === 'manual' && !row.label.trim()) out.set(row.uid, '워크스페이스 이름을 입력해 주세요.')
      else if (row.kind === 'manual' && !isCount(row.active)) out.set(row.uid, '활성은 0 이상의 정수로 입력해 주세요.')
      else if (row.kind === 'manual' && (names.get(nameKey(row.label)) ?? 0) > 1) out.set(row.uid, '같은 이름의 워크스페이스가 있습니다.')
    }
    return out
  }, [rows])

  const visible = useMemo(() => {
    const q = search.trim().toLowerCase()
    return rows.filter((row) => {
      // 방금 추가해 이름이 비어 있는 줄은 검색 중에도 보여 줍니다 — 안 보이면 입력할 수 없습니다.
      if (q && row.saved && !row.label.toLowerCase().includes(q) && !(row.workspace ?? '').includes(q)) return false
      if (scope === 'slack') return row.kind === 'slack'
      if (scope === 'manual') return row.kind === 'manual'
      if (scope === 'over') {
        const active = activeOf(row)
        return active !== null && active > toNumber(row.allocated)
      }
      return true
    })
  }, [rows, search, scope])

  const bars = useMemo(() => rows
    .filter((row) => row.label.trim())
    .map((row) => {
      const allocated = toNumber(row.allocated)
      const active = activeOf(row)
      return { row, allocated, active, rate: allocated > 0 && active !== null ? active / allocated : null }
    })
    .sort((a, b) => (b.rate ?? -1) - (a.rate ?? -1) || a.row.label.localeCompare(b.row.label, 'ko')), [rows])

  const totals = useMemo(() => ({
    allocated: rows.reduce((sum, row) => sum + toNumber(row.allocated), 0),
    active: rows.reduce((sum, row) => sum + (activeOf(row) ?? 0), 0),
  }), [rows])

  if (resource.loading && !report) return <Loading what="라이선스 현황을" />
  if (resource.error && !report) {
    return <Failed what="라이선스 현황을" detail={resource.error.message} onRetry={resource.reload} />
  }
  if (!report) return null

  const dirty = changed.length > 0 || removed.length > 0
  const blocked = problems.size > 0
  const failed = rows.filter((row) => row.kind === 'slack' && row.error)

  function patch(uid: string, change: Partial<Draft>) {
    setRows((current) => current.map((row) => (row.uid === uid ? { ...row, ...change } : row)))
  }

  function addRow() {
    const seq = newSeq + 1
    setNewSeq(seq)
    setScope('all')
    setRows((current) => [{
      uid: `new:${seq}`, kind: 'manual', id: null, workspace: null, label: '',
      allocated: '0', active: '0', slackActive: null, guests: 0, error: null, saved: null,
    }, ...current])
  }

  /** 직접 추가한 워크스페이스만 지웁니다. 저장 전까지는 되돌릴 수 있습니다(새로 고침). */
  function removeRow(row: Draft) {
    if (row.kind !== 'manual') return
    setRows((current) => current.filter((item) => item.uid !== row.uid))
    if (row.id !== null) setRemoved((current) => [...current, { id: row.id!, label: row.saved?.label ?? row.label }])
  }

  async function save() {
    if (saving || !dirty || blocked) return
    setSaving(true)
    setError(null)
    try {
      const result = await api.put<LicenseResponse>('/api/licenses', {
        rows: changed.filter((row) => row.kind === 'slack')
          .map((row) => ({ workspace: row.workspace, allocated: toNumber(row.allocated) })),
        manual: changed.filter((row) => row.kind === 'manual').map((row) => ({
          id: row.id, label: row.label.trim(), allocated: toNumber(row.allocated), active: toNumber(row.active),
        })),
        removed: removed.map((item) => item.id),
      })
      apply(result)
      onToast('라이선스 현황을 저장했습니다.')
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught))
    } finally {
      setSaving(false)
      setConfirmSave(false)
    }
  }

  function requestSave() {
    if (removed.length) setConfirmSave(true)
    else void save()
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
        note="워크스페이스별로 할당 라이선스와 실제 활성 수를 비교합니다."
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
          <div><div className="notice-title">라이선스 현황을 저장하지 못했습니다</div>
            <div className="notice-detail">{error}</div></div>
        </div>
      )}

      <Section title="워크스페이스별 라이선스 사용 현황 (할당 대비 활성)" aside={syncChip}>
        <div className="card card-pad">
          {bars.length ? <div className="license-bars">
            {bars.map(({ row, allocated, active, rate }) => (
              <div className="license-bar-row" key={row.uid}>
                <span className="license-bar-label">{row.label}<KindMark row={row} /></span>
                <span className="license-bar-track" aria-hidden="true">
                  <span className={`license-bar-fill ${rate !== null && rate >= 1 ? 'bad' : 'ok'}`}
                    style={{ width: `${rate === null ? 0 : Math.min(100, rate * 100)}%` }} />
                </span>
                <span className="license-bar-value">
                  {active === null ? '조회 실패' : `${fmt.int(active)}개`} / {fmt.int(allocated)}개
                  {rate !== null && ` (${Math.round(rate * 100)}%)`}
                </span>
              </div>
            ))}
          </div> : <p className="license-note">표시할 워크스페이스가 없습니다. 아래 「+ 워크스페이스 추가」 로 추가할 수 있습니다.</p>}
          {report.syncedAt && <p className="license-note">Slack 기준 시각 {fmt.dayClock(report.syncedAt)} · 활성은 비활성화되지 않은 사람 계정 수입니다(봇·단일 채널 게스트 제외). Slack 공정 청구로 빠지는 미접속 인원은 반영되지 않아 청구 수량보다 클 수 있습니다.</p>}
        </div>
      </Section>

      <Section
        title="워크스페이스별 상세 현황"
        lead={'할당 라이선스는 Slack 에서 가져올 수 없어 직접 입력합니다. 입력 후 "저장" 버튼을 눌러 반영하세요. Slack 연동 워크스페이스(🔗)의 활성은 자동으로 채워지고, 직접 추가한 워크스페이스는 활성도 직접 입력합니다. 삭제는 직접 추가한 워크스페이스만 할 수 있습니다.'}
        aside={<div className="license-tools">
          <input className="input" type="search" placeholder="워크스페이스 검색" aria-label="워크스페이스 검색"
            value={search} onChange={(event) => setSearch(event.target.value)} />
          <select className="input" aria-label="워크스페이스 범위" value={scope}
            onChange={(event) => setScope(event.target.value as Scope)}>
            <option value="all">전체 워크스페이스</option>
            <option value="slack">Slack 연동</option>
            <option value="manual">직접 추가</option>
            <option value="over">할당 초과</option>
          </select>
          <button className="btn" onClick={addRow}>+ 워크스페이스 추가</button>
          <button className="btn btn-primary" disabled={!dirty || saving || blocked} onClick={requestSave}>
            {saving ? '저장 중…' : '저장'}
          </button>
        </div>}
      >
        {(dirty || blocked) && <p className="license-dirty">
          {blocked
            ? [...new Set(problems.values())].join(' ')
            : `저장하지 않은 변경이 ${changed.length + removed.length}건 있습니다.`}
        </p>}
        <div className="card"><div className="table-wrap"><table className="table license-table">
          <thead><tr>
            <th>워크스페이스명</th><th className="num">할당(개)</th><th className="num">활성(개)</th>
            <th className="license-center">활성화율 / 초과</th><th className="right" aria-label="관리" />
          </tr></thead>
          <tbody>
            {visible.map((row) => {
              const active = activeOf(row)
              const problem = problems.get(row.uid)
              return <tr key={row.uid}>
                <td>{row.kind === 'manual'
                  ? <input className="input license-input license-name" aria-label="워크스페이스 이름" maxLength={80}
                    placeholder="워크스페이스 이름" autoFocus={!row.saved} value={row.label}
                    onChange={(event) => patch(row.uid, { label: event.target.value })} />
                  : row.label}
                  {row.guests > 0 && <div className="hint">다중 채널 게스트 {fmt.int(row.guests)}명 포함</div>}
                  {row.error && <div className="hint warn">{row.error}</div>}
                  {problem && <div className="hint bad">{problem}</div>}
                </td>
                <td className="num"><input className="input license-input" type="number" min="0" step="1" inputMode="numeric"
                  aria-label={`${row.label || '새 워크스페이스'} 할당 라이선스`} value={row.allocated}
                  onChange={(event) => patch(row.uid, { allocated: event.target.value })} /></td>
                <td className="num">{row.kind === 'manual'
                  ? <input className="input license-input" type="number" min="0" step="1" inputMode="numeric"
                    aria-label={`${row.label || '새 워크스페이스'} 활성 라이선스`} value={row.active}
                    onChange={(event) => patch(row.uid, { active: event.target.value })} />
                  : active === null ? <span className="hint">—</span> : fmt.int(active)}</td>
                <td className="license-center"><span className="license-rate-cell">
                  <RateBadge allocated={toNumber(row.allocated)} active={active} /><KindMark row={row} />
                </span></td>
                <td className="right">{row.kind === 'manual' &&
                  <button className="btn btn-sm btn-quiet" onClick={() => removeRow(row)}>✕ 삭제</button>}</td>
              </tr>
            })}
            {!visible.length && <tr><td colSpan={5}>{rows.length ? '조건에 맞는 워크스페이스가 없습니다.' : '표시할 워크스페이스가 없습니다.'}</td></tr>}
          </tbody>
          <tfoot><tr className="license-total">
            <td>전체 합계</td>
            <td className="num">{fmt.int(totals.allocated)}</td>
            <td className="num">{fmt.int(totals.active)}</td>
            <td className="license-center"><RateBadge allocated={totals.allocated} active={totals.active} /></td>
            <td />
          </tr></tfoot>
        </table></div></div>
      </Section>

      <ConfirmDialog open={confirmSave} danger title="삭제를 포함해 저장할까요?"
        detail={`직접 추가한 워크스페이스 ${removed.map((item) => item.label).join(', ')} 을(를) 지웁니다. 입력했던 할당·활성 값도 함께 지워지며 되돌릴 수 없습니다. 실행자는 감사 기록에 남습니다.`}
        confirmLabel="삭제하고 저장" busy={saving}
        onCancel={() => setConfirmSave(false)} onConfirm={() => { void save() }} />
    </>
  )
}
