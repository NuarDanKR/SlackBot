/** 승인 영역 — 배포 요청과 규칙 편집 요청이 함께 씁니다.
 *
 * 승인자는 어드민(owner) 한 사람입니다. 그래서 화면을 두 갈래로만 나눕니다.
 *   - 어드민:  검사가 통과했으면 [승인하고 반영] · [반려] 버튼이 보입니다.
 *   - 현업:    버튼 없이 "승인 대기 중" 상태만 보입니다.
 * 예전처럼 요청·검사·승인 세 칸을 도장으로 그리지 않습니다. 승인 주체가 한 명이면
 * 그 형식은 정보를 주지 않고 화면만 무겁게 만듭니다.
 */
import type { Check, ConsoleUser } from '../types'

const MARK: Record<Check['state'], string> = {
  pass: '✓',
  fail: '✕',
  running: '◍',
  pending: '·',
}

export function Checks({ checks, label }: { checks: Check[]; label: string }) {
  return (
    <>
      <div className="col-label">{label}</div>
      <div className="checks">
        {checks.map((c) => (
          <div className={`check ${c.state}`} key={c.id}>
            <span className="check-mark" aria-hidden="true">
              {MARK[c.state]}
            </span>
            <span className="check-name">{c.label}</span>
            <span className="check-detail">{c.detail}</span>
          </div>
        ))}
      </div>
    </>
  )
}

export function gate(checks: Check[]) {
  const failed = checks.filter((c) => c.state === 'fail')
  const running = checks.filter((c) => c.state === 'running' || c.state === 'pending')
  return { failed, running, passed: failed.length === 0 && running.length === 0 }
}

export function statusChip(checks: Check[], approved: boolean) {
  const { failed, passed } = gate(checks)
  if (approved) return <span className="chip ok">반영 완료</span>
  if (failed.length) return <span className="chip bad">검사 실패</span>
  if (passed) return <span className="chip watch">승인 대기</span>
  return <span className="chip info">검사 중</span>
}

export function ApprovalBox({
  user,
  checks,
  approved,
  requester,
  requestedAt,
  approveLabel,
  onApprove,
  onReject,
  afterNote,
  blockedNote,
}: {
  user: ConsoleUser
  checks: Check[]
  approved: boolean
  requester: string
  requestedAt: string
  approveLabel: string
  onApprove: () => void
  onReject: () => void
  /** 승인 후 무슨 일이 일어나는지 알려주는 안내문 */
  afterNote: string
  /** 검사 실패 시 안내문 */
  blockedNote: string
}) {
  const { failed, running, passed } = gate(checks)
  const isOwner = user.role === 'owner'

  if (approved) {
    return (
      <div className="approve-box">
        <div className="approve-status done">승인되었습니다. 반영을 시작합니다.</div>
        <p className="hint">{afterNote}</p>
      </div>
    )
  }

  // 현업(member) 화면 — 버튼 없이 상태만 보여 줍니다.
  if (!isOwner) {
    return (
      <div className="approve-box">
        <div className="approve-status wait">
          {failed.length ? '검사에서 막혔습니다' : passed ? '승인 대기 중' : '자동 검사 진행 중'}
        </div>
        <p className="hint">
          {failed.length ? (
            blockedNote
          ) : passed ? (
            <>
              요청이 접수되었습니다. 관리자가 확인하면 자동으로 반영되고, 결과는 이 화면과 Slack
              알림으로 알려 드립니다. 반영 전까지 봇 동작은 지금 그대로 유지됩니다.
            </>
          ) : (
            <>자동 검사가 끝나면 관리자 확인 단계로 넘어갑니다. 보통 1~2분 걸립니다.</>
          )}
        </p>
        <p className="hint">
          요청자 {requester} · {requestedAt}
        </p>
      </div>
    )
  }

  // 어드민(owner) 화면 — 검사를 통과한 요청만 승인 버튼이 열립니다.
  return (
    <div className="approve-box">
      <button className="btn btn-primary btn-block" disabled={!passed} onClick={onApprove}>
        {approveLabel}
      </button>
      <button className="btn btn-danger btn-block" onClick={onReject}>
        반려
      </button>
      {failed.length ? (
        <p className="hint bad">{blockedNote}</p>
      ) : running.length ? (
        <p className="hint">자동 검사가 끝나면 승인 버튼이 열립니다.</p>
      ) : (
        <p className="hint">{afterNote}</p>
      )}
      <p className="hint">
        요청자 {requester} · {requestedAt}
      </p>
    </div>
  )
}
