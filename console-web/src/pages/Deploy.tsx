import { useEffect, useMemo, useState } from 'react'
import { ApiError, api } from '../api/client'
import { useResource } from '../api/hooks'
import { Chip, Failed, Loading, PageHead, Section, fmt } from '../components/primitives'
import type { ConsoleUser, DeployRequest } from '../types'

type RuntimeState = 'idle' | 'queued' | 'running' | 'ok' | 'failed' | 'skipped'

interface DeploymentStatus {
  state: RuntimeState
  pending: boolean
  actor: string
  before: string
  after: string
  beforeTitle: string
  afterTitle: string
  message: string
  detail: string
  requestedAt: string | null
  startedAt: string | null
  finishedAt: string | null
  approvalId: number | null
}

interface RequestResponse { requests: DeployRequest[] }
interface StatusResponse { workspaces: { key: string; label: string }[] }

const RUNTIME_LABEL: Record<RuntimeState, string> = {
  idle: '배포 기록 없음', queued: '배포 대기 중', running: '배포 진행 중',
  ok: '최근 배포 성공', failed: '최근 배포 실패', skipped: '새 변경 없음',
}
const REQUEST_LABEL: Record<DeployRequest['state'], string> = {
  awaiting_checks: '검사 중', awaiting_approval: '승인 대기', blocked: '실패',
  approved: '승인됨', applying: '배포 중', live: '반영 완료', rejected: '반려', rolled_back: '되돌림',
}

function runtimeChip(state: RuntimeState) {
  if (state === 'ok') return <Chip tone="ok">{RUNTIME_LABEL[state]}</Chip>
  if (state === 'failed') return <Chip tone="bad">{RUNTIME_LABEL[state]}</Chip>
  if (state === 'queued' || state === 'running') return <Chip tone="watch">{RUNTIME_LABEL[state]}</Chip>
  return <Chip tone="plain">{RUNTIME_LABEL[state]}</Chip>
}

function requestChip(state: DeployRequest['state']) {
  if (state === 'live') return <Chip tone="ok">{REQUEST_LABEL[state]}</Chip>
  if (state === 'blocked' || state === 'rejected' || state === 'rolled_back') {
    return <Chip tone="bad">{REQUEST_LABEL[state]}</Chip>
  }
  if (state === 'awaiting_approval' || state === 'approved' || state === 'applying') {
    return <Chip tone="watch">{REQUEST_LABEL[state]}</Chip>
  }
  return <Chip tone="plain">{REQUEST_LABEL[state]}</Chip>
}

export function Deploy({ user, onToast }: { user: ConsoleUser; onToast: (message: string) => void }) {
  const runtime = useResource<DeploymentStatus>('/api/deployment')
  const queue = useResource<RequestResponse>('/api/deploy-requests')
  const status = useResource<StatusResponse>('/api/status')
  const [requests, setRequests] = useState<DeployRequest[]>([])
  const [workspace, setWorkspace] = useState('')
  const [reason, setReason] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => { if (queue.data) setRequests(queue.data.requests) }, [queue.data])
  useEffect(() => {
    if (!workspace && status.data?.workspaces.length) setWorkspace(status.data.workspaces[0].key)
  }, [status.data, workspace])
  useEffect(() => {
    const busy = runtime.data?.state === 'queued' || runtime.data?.state === 'running'
    const open = requests.some((item) => ['approved', 'applying'].includes(item.state))
    if (!busy && !open) return
    const timer = window.setInterval(() => { runtime.reload(); queue.reload() }, 5000)
    return () => window.clearInterval(timer)
  }, [requests, runtime.data?.state, runtime.reload, queue.reload])

  const visibleWorkspaces = useMemo(() => status.data?.workspaces ?? [], [status.data])

  async function createRequest() {
    if (!workspace || reason.trim().length < 5 || submitting) return
    setSubmitting(true); setError(null)
    try {
      const result = await api.put<RequestResponse>('/api/deploy-requests', {
        workspace, reason: reason.trim(),
      })
      setRequests(result.requests); setReason('')
      onToast('배포 요청을 등록했습니다. 다른 관리자의 승인을 기다립니다.')
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught))
    } finally { setSubmitting(false) }
  }

  async function decide(id: string, decision: 'approve' | 'reject') {
    const action = decision === 'approve' ? '승인' : '반려'
    if (!window.confirm(`이 배포 요청을 ${action}하시겠습니까?`)) return
    setSubmitting(true); setError(null)
    try {
      const result = await api.put<RequestResponse>(`/api/deploy-requests/${id}/decision`, {
        decision, note: `${user.email} 콘솔 ${action}`,
      })
      setRequests(result.requests)
      runtime.reload()
      onToast(decision === 'approve' ? '승인했습니다. 서버 배포를 시작합니다.' : '배포 요청을 반려했습니다.')
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught))
    } finally { setSubmitting(false) }
  }

  if ((runtime.loading || queue.loading) && !runtime.data && !queue.data) return <Loading what="배포 상태를" />
  if (runtime.error && !runtime.data) return <Failed what="배포 상태를" detail={runtime.error.message} onRetry={runtime.reload} />
  if (queue.error && !queue.data) return <Failed what="배포 요청을" detail={queue.error.message} onRetry={queue.reload} />
  const current = runtime.data

  return (
    <>
      <PageHead crumb="운영 관리 · 배포" title="배포 관리"
        note="개발자가 변경 이유와 담당 워크스페이스를 지정해 요청하면, 요청자가 아닌 관리자가 승인한 뒤 서버의 전체 테스트를 통과한 코드만 배포합니다."
        aside={current ? runtimeChip(current.state) : <Chip tone="plain">상태 없음</Chip>} />

      {error && <div className="notice bad"><div className="notice-kind">처리 실패</div><div>
        <div className="notice-title">배포 요청을 처리하지 못했습니다</div><div className="notice-detail">{error}</div>
      </div></div>}

      <Section title="배포 요청 등록" lead="자신이 등록한 요청은 직접 승인할 수 없습니다. 최소 두 명의 관리자가 있어야 관리자의 변경도 배포할 수 있습니다.">
        <div className="card card-pad">
          <div className="form-grid">
            <div className="field"><label className="field-label" htmlFor="deploy-workspace">담당 워크스페이스</label>
              <select id="deploy-workspace" className="input" value={workspace}
                onChange={(event) => setWorkspace(event.target.value)}>
                {visibleWorkspaces.map((item) => <option value={item.key} key={item.key}>{item.label} ({item.key})</option>)}
              </select></div>
            <div className="field"><label className="field-label" htmlFor="deploy-reason">변경 이유</label>
              <input id="deploy-reason" className="input" value={reason} placeholder="무엇을 왜 배포하는지 입력"
                maxLength={500} onChange={(event) => setReason(event.target.value)} /></div>
          </div>
          <div className="form-row"><button className="btn btn-primary" disabled={!workspace || reason.trim().length < 5 || submitting}
            onClick={createRequest}>{submitting ? '처리 중…' : '승인 요청'}</button></div>
        </div>
      </Section>

      <Section title="승인 요청" note={`${requests.length}건`} lead="처리 중인 요청이 먼저 표시됩니다. 승인 후에도 update.sh의 테스트와 fast-forward 검사를 통과해야 운영 코드가 바뀝니다.">
        <div className="card card-pad"><div className="table-scroll"><table className="table">
          <thead><tr><th>요청</th><th>워크스페이스</th><th>기준 커밋</th><th>요청자</th><th>상태</th><th className="right">처리</th></tr></thead>
          <tbody>{requests.map((item) => {
            const canDecide = user.role === 'admin' && item.state === 'awaiting_approval' && item.requester.toLowerCase() !== user.email.toLowerCase()
            return <tr key={item.id}><td><div>{item.commitTitle}</div><div className="hint">{fmt.dayClock(item.requestedAt)}</div></td>
              <td>{item.workspaceLabel}<div className="hint mono">{item.workspace}</div></td>
              <td><span className="mono">{item.commit.slice(0, 8)}</span><div className="hint mono">{item.branch}</div></td>
              <td>{item.requester}</td><td>{requestChip(item.state)}</td><td className="right">
                {canDecide ? <><button className="btn btn-sm btn-primary" disabled={submitting} onClick={() => decide(item.id, 'approve')}>승인</button>{' '}
                  <button className="btn btn-sm btn-danger" disabled={submitting} onClick={() => decide(item.id, 'reject')}>반려</button></>
                  : item.state === 'awaiting_approval' && item.requester.toLowerCase() === user.email.toLowerCase()
                    ? <span className="hint">본인 요청</span> : '-'}</td></tr>
          })}
          {!requests.length && <tr><td colSpan={6}>등록된 배포 요청이 없습니다.</td></tr>}</tbody>
        </table></div></div>
      </Section>

      {current && <Section title="최근 서버 배포" lead="실제 root 배포 러너의 상태와 결과입니다.">
        <div className="card card-pad"><div className="card-head"><div><div className="card-title">{RUNTIME_LABEL[current.state]}</div>
          <p className="hint">{current.message || '아직 기록된 배포 결과가 없습니다.'}</p></div>{runtimeChip(current.state)}</div>
          <div className="deploy-actor"><span className="metric-label">실행 승인자</span> {current.actor || '-'}</div>
          <div className="deploy-diff">
            <div className="deploy-diff-side is-before">
              <div className="metric-label">배포 전</div>
              <div className="mono deploy-diff-sha">{current.before || '-'}</div>
              <div className="deploy-commit-title" title={current.beforeTitle}>{current.beforeTitle || '-'}</div>
            </div>
            <div className="deploy-diff-arrow" aria-hidden="true">→</div>
            <div className="deploy-diff-side is-after">
              <div className="metric-label">배포 후</div>
              <div className="mono deploy-diff-sha">{current.after || '-'}</div>
              <div className="deploy-commit-title" title={current.afterTitle}>{current.afterTitle || '-'}</div>
            </div>
          </div>
          {current.state === 'failed' && current.detail && <div className="deploy-failure"><div className="metric-label">실패 사유</div><pre>{current.detail}</pre></div>}
          <div className="hint" style={{ marginTop: 16 }}>요청 {current.requestedAt ? fmt.dayClock(current.requestedAt) : '-'} · 시작 {current.startedAt ? fmt.dayClock(current.startedAt) : '-'} · 완료 {current.finishedAt ? fmt.dayClock(current.finishedAt) : '-'}</div>
        </div>
      </Section>}
    </>
  )
}
