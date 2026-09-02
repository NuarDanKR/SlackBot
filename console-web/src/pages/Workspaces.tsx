import { useEffect, useState } from 'react'
import { ApiError, api } from '../api/client'
import { useResource } from '../api/hooks'
import { SetupGuide } from '../components/SetupGuide'
import { Chip, Failed, Loading, PageHead, Section, fmt } from '../components/primitives'

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

interface WorkspaceResponse {
  workspaces: WorkspaceEntry[]
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

export function Workspaces({ onToast }: { onToast: (message: string) => void }) {
  const resource = useResource<WorkspaceResponse>('/api/workspaces')
  const [rows, setRows] = useState<WorkspaceEntry[]>([])
  const [draft, setDraft] = useState<Draft>(EMPTY)
  const [editing, setEditing] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (resource.data) setRows(resource.data.workspaces)
  }, [resource.data])

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
    if (!window.confirm(`${draft.label.trim()} 설정을 저장하고 TYBot 재시작을 요청하시겠습니까?`)) return
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
      onToast(`${draft.label.trim()} 설정을 저장했습니다. TYBot이 1분 안에 재시작됩니다.`)
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
        crumb="관리자 설정 · 워크스페이스"
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
            <button className="btn btn-primary" disabled={!ready || saving} onClick={save}>
              {saving ? '저장 중…' : editingRow?.tokenInEnv ? 'DB로 이전 및 저장' : editing ? '변경 저장' : '워크스페이스 등록'}
            </button>
            {editing && <button className="btn btn-quiet" onClick={reset}>취소</button>}
          </div>
        </div>
      </Section>

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
                <td className="num">${row.limitUsd.toFixed(2)}</td>
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
    </>
  )
}
