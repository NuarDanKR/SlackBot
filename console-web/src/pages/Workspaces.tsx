import { useEffect, useState } from 'react'
import { ApiError, api } from '../api/client'
import { useResource } from '../api/hooks'
import { SetupGuide } from '../components/SetupGuide'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { Chip, Failed, Loading, Metric, PageHead, Section, fmt } from '../components/primitives'

type WorkspaceRole = 'root' | 'member'
type WorkspaceState = 'enabled' | 'disabled' | 'error'

interface WorkspaceEntry {
  key: string
  label: string
  role: WorkspaceRole
  state: WorkspaceState
  error: string | null
  limitUsd: number
  readable: string[]
  botTokenMask: string
  appTokenMask: string
  secretUpdatedAt: string | null
  secretUpdatedBy: string
  /** DB 토큰이 없고 이전 가능한 환경변수 토큰이 있습니다. */
  tokenInEnv: boolean
  archivePath: string
  createdAt: string
  createdBy: string
}

/** 상한은 두 겹입니다. 전체 상한이 바깥 테두리, 워크스페이스 상한이 그 안쪽 칸입니다. */
interface Budget {
  /** DAILY_COST_LIMIT_USD — 결제 계정의 뚜껑. 서버 설정 파일에서만 바꿉니다. */
  globalLimitUsd: number
  /** 상한이 설정된 워크스페이스들의 합계. */
  workspaceTotalUsd: number
  configuredCount: number
  unlimitedCount: number
  /** 합계가 전체 상한을 넘었습니다 — 안쪽 칸은 닿을 수 없습니다. */
  overcommitted: boolean
  spentTodayUsd: number
  spentByWorkspace: Record<string, number>
}

interface WorkspaceResponse {
  workspaces: WorkspaceEntry[]
  budget?: Budget
  restartPending?: boolean
}

interface Draft {
  key: string
  label: string
  role: WorkspaceRole
  state: 'enabled' | 'disabled'
  limitUsd: string
  readable: string[]
  botToken: string
  appToken: string
}

const EMPTY: Draft = {
  key: '', label: '', role: 'member', state: 'enabled', limitUsd: '2',
  readable: [], botToken: '', appToken: '',
}
const KEY_RE = /^[a-z][a-z0-9-]{1,23}$/

function editDraft(row: WorkspaceEntry): Draft {
  return {
    key: row.key,
    label: row.label,
    role: row.role,
    state: row.state === 'disabled' ? 'disabled' : 'enabled',
    limitUsd: String(row.limitUsd),
    readable: [...row.readable],
    botToken: '',
    appToken: '',
  }
}

function stateChip(row: WorkspaceEntry) {
  if (row.state === 'enabled') return <Chip tone="ok">동작 중</Chip>
  if (row.state === 'error') return <Chip tone="bad">연결 오류</Chip>
  return <Chip tone="plain">사용 중지</Chip>
}

/** 상한의 80% 를 넘으면 눈에 띄게 합니다 — 닿고 나서 알면 이미 답변이 멈춘 뒤입니다. */
function spentTone(row: WorkspaceEntry, budget: Budget) {
  const spent = budget.spentByWorkspace[row.key] ?? 0
  const limit = row.limitUsd > 0 ? row.limitUsd : budget.globalLimitUsd
  if (limit > 0 && spent >= limit) return 'hint bad'
  if (limit > 0 && spent >= limit * 0.8) return 'hint warn'
  return 'hint'
}

/**
 * 입력한 상한이 실제로 먹는지 미리 말합니다.
 *
 * 저장한 뒤에야 "여전히 막힌다" 를 겪게 두지 않습니다. 이 값이 전체 상한 안에
 * 들어가는지는 **여기서 이미 알 수 있습니다.**
 */
function limitHelp(draft: Draft, rows: WorkspaceEntry[], budget: Budget) {
  const value = Number(draft.limitUsd)
  if (!Number.isFinite(value) || value <= 0) {
    return {
      tone: 'field-help',
      text: `0 은 상한 없음입니다 — 전체 상한 ${fmt.usd(budget.globalLimitUsd)} 까지 혼자 쓸 수 있습니다.`,
    }
  }
  // 나 자신은 빼고 센다. 편집 중이면 저장 전 값이 합계에 들어 있다.
  const others = rows
    .filter((row) => row.key !== draft.key && row.limitUsd > 0)
    .reduce((sum, row) => sum + row.limitUsd, 0)
  const total = others + value
  if (budget.globalLimitUsd > 0 && value > budget.globalLimitUsd) {
    return {
      tone: 'field-help warn',
      text: `전체 상한 ${fmt.usd(budget.globalLimitUsd)} 보다 큽니다 — 이 값은 닿을 수 없고 전체 상한에서 먼저 막힙니다.`,
    }
  }
  if (budget.globalLimitUsd > 0 && total > budget.globalLimitUsd) {
    return {
      tone: 'field-help warn',
      text: `다른 워크스페이스와 합치면 ${fmt.usd(total)} 로 전체 상한 ${fmt.usd(budget.globalLimitUsd)} 를 넘습니다 — 여러 곳이 같은 날 많이 쓰면 전체 상한에서 막힙니다.`,
    }
  }
  return {
    tone: 'field-help',
    text: `전체 상한 ${fmt.usd(budget.globalLimitUsd)} · 다른 워크스페이스 합계 ${fmt.usd(others)}. 이 값 변경은 재시작 없이 1분 안에 적용됩니다.`,
  }
}

/**
 * 두 겹의 상한을 한 자리에서 봅니다.
 *
 * 2026-09-15 에 경영본부가 막혔을 때, 이 화면에는 워크스페이스 상한($10)만 있었고
 * 실제로 막은 전체 상한($5)은 **어디에도 없었습니다.** 사람이 볼 수 있는 정보만으로는
 * 원인에 닿을 수 없었고, 그래서 안 먹는 숫자를 세 번 올렸습니다.
 */
function BudgetSummary({ budget }: { budget: Budget }) {
  const headroom = budget.globalLimitUsd - budget.workspaceTotalUsd
  return (
    <Section
      title="하루 사용 상한"
      lead="상한은 두 겹입니다. 전체 상한이 결제 계정의 뚜껑이고, 워크스페이스 상한은 그 안에서 한 팀이 쓸 수 있는 몫입니다. 둘 중 먼저 닿는 쪽이 답변을 멈춥니다."
    >
      <div className="card card-pad">
        <div className="metrics">
          <Metric k="전체 상한" v={fmt.usd(budget.globalLimitUsd)} />
          <Metric k="워크스페이스 상한 합계" v={fmt.usd(budget.workspaceTotalUsd)} />
          <Metric k="오늘 사용" v={fmt.usd(budget.spentTodayUsd)} />
          <Metric k="상한 미설정" v={String(budget.unlimitedCount)} unit="개" />
        </div>

        {budget.overcommitted ? (
          <div className="notice warn" style={{ marginTop: 14 }}>
            <div className="notice-kind">상한 어긋남</div>
            <div>
              <div className="notice-title">
                워크스페이스 상한 합계({fmt.usd(budget.workspaceTotalUsd)})가 전체 상한
                ({fmt.usd(budget.globalLimitUsd)})보다 큽니다
              </div>
              <div className="notice-detail">
                아래에서 상한을 올려도 전체 상한에서 먼저 막힙니다. 서버 설정 파일의
                <span className="mono"> DAILY_COST_LIMIT_USD </span>
                를 올리고 TYBot을 재시작해야 합니다.
              </div>
            </div>
          </div>
        ) : (
          <p className="note">
            전체 상한까지 {fmt.usd(Math.max(0, headroom))} 남았습니다.
            {budget.unlimitedCount > 0 && ' 상한을 정하지 않은 워크스페이스는 전체 상한까지 혼자 쓸 수 있습니다.'}
          </p>
        )}
      </div>
    </Section>
  )
}

export function Workspaces({ selectedKey, onToast }: { selectedKey?: string | null; onToast: (message: string) => void }) {
  const resource = useResource<WorkspaceResponse>('/api/workspaces')
  const [rows, setRows] = useState<WorkspaceEntry[]>([])
  const [budget, setBudget] = useState<Budget | null>(null)
  const [draft, setDraft] = useState<Draft>(EMPTY)
  const [editing, setEditing] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [confirmSave, setConfirmSave] = useState(false)

  useEffect(() => {
    if (resource.data) {
      setRows(resource.data.workspaces)
      setBudget(resource.data.budget ?? null)
    }
  }, [resource.data])

  useEffect(() => {
    if (!selectedKey || !resource.data) return
    const selected = resource.data.workspaces.find((row) => row.key === selectedKey)
    if (selected) {
      setDraft(editDraft(selected))
      setEditing(true)
    }
  }, [resource.data, selectedKey])

  if (resource.loading && !resource.data) return <Loading what="워크스페이스 목록을" />
  if (resource.error && !resource.data) {
    return <Failed what="워크스페이스 목록을" detail={resource.error.message} onRetry={resource.reload} />
  }

  const tokenPair = Boolean(draft.botToken) === Boolean(draft.appToken)
  const editingRow = editing ? rows.find((row) => row.key === draft.key) : undefined
  const ready = KEY_RE.test(draft.key) && draft.label.trim().length > 0 && tokenPair &&
    (editing || (draft.botToken.startsWith('xoxb-') && draft.appToken.startsWith('xapp-')))

  function reset() {
    setDraft(EMPTY)
    setEditing(false)
    setError(null)
  }

  function toggleReadable(key: string, checked: boolean) {
    setDraft((current) => ({
      ...current,
      readable: checked
        ? [...new Set([...current.readable, key])]
        : current.readable.filter((value) => value !== key),
    }))
  }

  async function save() {
    if (!ready || saving) return
    setSaving(true)
    setError(null)
    try {
      const body: Record<string, unknown> = {
        label: draft.label.trim(), role: draft.role, state: draft.state,
        limitUsd: Number(draft.limitUsd), readable: draft.readable,
      }
      if (draft.botToken && draft.appToken) {
        body.botToken = draft.botToken
        body.appToken = draft.appToken
      }
      const result = await api.put<WorkspaceResponse>(
        `/api/workspaces/${encodeURIComponent(draft.key)}`,
        body,
      )
      setRows(result.workspaces)
      setBudget(result.budget ?? null)
      // 상한만 바꿨으면 재시작하지 않습니다. 매번 재시작한다고 안내하면
      // 사람은 상한 한 칸 고치자고 답변이 끊기는 줄 알고 아예 안 고칩니다.
      onToast(result.restartPending === false
        ? `${draft.label.trim()} 상한을 저장했습니다. 재시작 없이 1분 안에 적용됩니다.`
        : `${draft.label.trim()} 설정을 저장했습니다. TYBot이 1분 안에 재시작됩니다.`)
      reset()
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught))
    } finally {
      setSaving(false)
    }
  }

  return (
    <>
      <PageHead
        crumb="운영 · 워크스페이스"
        title="워크스페이스 관리"
        note="새 Slack 앱을 등록하고 표시 이름, 열람 범위, 사용 상태와 토큰을 관리합니다. 저장된 토큰 원문은 다시 표시되지 않습니다."
        aside={<Chip tone="plain">등록 {rows.length}개</Chip>}
      />

      <div className="section"><SetupGuide /></div>

      {error && (
        <div className="notice bad">
          <div className="notice-kind">저장 실패</div>
          <div><div className="notice-title">워크스페이스 설정을 저장하지 못했습니다</div>
            <div className="notice-detail">{error}</div></div>
        </div>
      )}

      <Section
        title={editing ? `${draft.label} 설정 편집` : '새 워크스페이스 등록'}
        lead={editing
          ? editingRow?.tokenInEnv
            ? '저장하면 현재 서버 설정 파일의 두 토큰을 암호화해 DB로 이전합니다. 새 토큰으로 교체하려면 두 토큰을 함께 입력하세요.'
            : '토큰 입력란을 비워 두면 기존 DB 토큰을 유지합니다. 교체할 때는 두 토큰을 함께 입력해야 합니다.'
          : 'Slack 앱에서 받은 봇 토큰과 앱 토큰이 모두 있어야 등록할 수 있습니다.'}
      >
        <div className="card card-pad">
          <div className="form-grid">
            <div className="field">
              <label className="field-label" htmlFor="ws-key">키</label>
              <input id="ws-key" className="input mono" value={draft.key} disabled={editing}
                placeholder="tyit"
                onChange={(event) => setDraft({ ...draft, key: event.target.value.trim().toLowerCase() })} />
              <span className="field-help">소문자로 시작하는 2~24자의 소문자·숫자·하이픈</span>
            </div>
            <div className="field">
              <label className="field-label" htmlFor="ws-label">표시 이름</label>
              <input id="ws-label" className="input" value={draft.label} placeholder="전산팀"
                onChange={(event) => setDraft({ ...draft, label: event.target.value })} />
            </div>
            <div className="field">
              <label className="field-label" htmlFor="ws-role">등급</label>
              <select id="ws-role" className="input" value={draft.role}
                onChange={(event) => setDraft({ ...draft, role: event.target.value as WorkspaceRole })}>
                <option value="member">일반 워크스페이스</option>
                <option value="root">상위 워크스페이스</option>
              </select>
            </div>
            <div className="field">
              <label className="field-label" htmlFor="ws-state">상태</label>
              <select id="ws-state" className="input" value={draft.state}
                onChange={(event) => setDraft({ ...draft, state: event.target.value as Draft['state'] })}>
                <option value="enabled">사용</option><option value="disabled">사용 중지</option>
              </select>
            </div>
            <div className="field">
              <label className="field-label" htmlFor="ws-limit">하루 사용 상한 (USD)</label>
              <input id="ws-limit" className="input mono" type="number" min="0" max="10000"
                step="0.1" value={draft.limitUsd}
                onChange={(event) => setDraft({ ...draft, limitUsd: event.target.value })} />
              {budget && <span className={limitHelp(draft, rows, budget).tone}>
                {limitHelp(draft, rows, budget).text}
              </span>}
            </div>
            <div className="field">
              <label className="field-label" htmlFor="ws-bot">봇 토큰</label>
              <input id="ws-bot" className="input mono" type="password" autoComplete="off"
                placeholder={editing ? '변경할 때만 입력' : 'xoxb-'} value={draft.botToken}
                onChange={(event) => setDraft({ ...draft, botToken: event.target.value })} />
            </div>
            <div className="field">
              <label className="field-label" htmlFor="ws-app">앱 토큰</label>
              <input id="ws-app" className="input mono" type="password" autoComplete="off"
                placeholder={editing ? '변경할 때만 입력' : 'xapp-'} value={draft.appToken}
                onChange={(event) => setDraft({ ...draft, appToken: event.target.value })} />
            </div>
          </div>

          <div className="field" style={{ marginTop: 18 }}>
            <span className="field-label">크로스 워크스페이스 열람 대상</span>
            <div className="env-readable">
              {rows.filter((row) => row.key !== draft.key).map((row) => (
                <label className="check-line compact" key={row.key}>
                  <input type="checkbox" checked={draft.readable.includes(row.key)}
                    onChange={(event) => toggleReadable(row.key, event.target.checked)} />
                  <span>{row.label}</span>
                </label>
              ))}
              {!rows.length && <span className="field-help">등록된 다른 워크스페이스가 없습니다.</span>}
            </div>
          </div>

          <div className="form-row">
            <button className="btn btn-primary" disabled={!ready || saving} onClick={() => setConfirmSave(true)}>
              {saving ? '저장 중…' : editingRow?.tokenInEnv ? 'DB로 이전 및 저장' : editing ? '변경 저장' : '워크스페이스 등록'}
            </button>
            {editing && <button className="btn btn-quiet" onClick={reset}>취소</button>}
          </div>
        </div>
      </Section>

      {budget && <BudgetSummary budget={budget} />}

      <Section title="등록된 워크스페이스"
        lead="토큰은 마스킹된 값과 마지막 교체 정보만 표시됩니다. 삭제 대신 사용 중지를 지원합니다.">
        <div className="card card-pad"><div className="table-scroll"><table className="table">
          <thead><tr><th>워크스페이스</th><th>등급</th><th>상태</th><th>토큰</th>
            <th>열람 대상</th><th className="num">상한</th><th className="right">관리</th></tr></thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.key}>
                <td><div>{row.label}</div><div className="hint mono">{row.key}</div></td>
                <td>{row.role === 'root' ? '상위' : '일반'}</td>
                <td>{stateChip(row)}{row.error && <div className="hint warn">{row.error}</div>}</td>
                <td><div className="mono">{row.botTokenMask}</div><div className="mono">{row.appTokenMask}</div>
                  <div className="hint">{row.tokenInEnv
                    ? '환경변수 사용 중 · 편집 후 저장하면 암호화 DB로 이전됩니다.'
                    : `${row.secretUpdatedAt ? fmt.dayClock(row.secretUpdatedAt) : '교체 기록 없음'} · ${row.secretUpdatedBy}`}</div></td>
                <td>{row.readable.length ? row.readable.join(' · ') : '-'}</td>
                <td className="num">
                  {row.limitUsd > 0 ? fmt.usd(row.limitUsd) : <span className="hint">미설정</span>}
                  {budget && <div className={spentTone(row, budget)}>
                    오늘 {fmt.usd(budget.spentByWorkspace[row.key] ?? 0)}
                  </div>}
                </td>
                <td className="right"><button className="btn btn-sm btn-quiet" onClick={() => {
                  setDraft(editDraft(row)); setEditing(true); setError(null)
                  window.scrollTo({ top: 0, behavior: 'smooth' })
                }}>편집</button></td>
              </tr>
            ))}
            {!rows.length && <tr><td colSpan={7}>등록된 워크스페이스가 없습니다.</td></tr>}
          </tbody>
        </table></div></div>
      </Section>
      <ConfirmDialog open={confirmSave} title={`${draft.label.trim()} 설정을 저장할까요?`}
        detail="워크스페이스 설정과 토큰 변경 사항을 저장한 뒤 TYBot 재시작을 요청합니다. 저장된 토큰 원문은 다시 표시되지 않으며 실행자는 감사 기록에 남습니다."
        confirmLabel={editing ? '변경 저장' : '워크스페이스 등록'} busy={saving}
        onCancel={() => setConfirmSave(false)} onConfirm={() => { void save().finally(() => setConfirmSave(false)) }} />
    </>
  )
}
