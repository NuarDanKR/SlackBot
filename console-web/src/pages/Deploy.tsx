/** 배포 승인 — 이 콘솔에서 유일하게 서버를 바꾸는 화면입니다.
 *
 * 실행할 수 있는 동작은 반영 / 재기동 / 되돌리기 셋뿐입니다. 명령을 직접 입력하는 칸은
 * 두지 않습니다. 승인 권한은 관리자 한 사람에게만 있습니다.
 */
import { useState } from 'react'
import { deployHistory, deployRequests } from '../mock/data'
import type { ConsoleUser, DeployRequest } from '../types'
import { ApprovalBox, Checks, statusChip } from '../components/Approval'
import { Empty, MockBadge, PageHead, Section, fmt } from '../components/primitives'

const ACTION_CLASS: Record<string, string> = {
  적용: 'is-apply',
  롤백: 'is-rollback',
  반려: 'is-reject',
}

function RequestCard({
  r,
  user,
  onToast,
}: {
  r: DeployRequest
  user: ConsoleUser
  onToast: (m: string) => void
}) {
  const [approved, setApproved] = useState(false)

  return (
    <article className="request">
      <div className="request-head">
        <div>
          <div className="request-title">{r.commitTitle}</div>
          <div className="request-meta">
            <span>{r.workspaceLabel}</span>
            <span className="sep">·</span>
            <span className="mono">{r.repo}</span>
            <span className="sep">·</span>
            <span className="mono">
              {r.branch} / {r.commit}
            </span>
            <span className="sep">·</span>
            <span>{r.requester} 요청</span>
          </div>
        </div>
        {statusChip(r.checks, approved)}
      </div>

      <div className="request-body">
        <div className="request-col">
          <Checks checks={r.checks} label="자동 검사 — 네 항목이 모두 통과해야 반영할 수 있습니다" />

          <div className="files">
            <div className="col-label">변경된 파일 {r.filesChanged.length}개</div>
            {r.filesChanged.map((f) => (
              <div className="file-row" key={f.path}>
                <span className="file-path">{f.path}</span>
                <span className="file-delta">
                  <span className="add">+{f.added}</span> <span className="del">−{f.removed}</span>
                </span>
              </div>
            ))}
            {!r.fastForward && (
              <p className="hint bad" style={{ marginTop: 10 }}>
                커밋 이력을 다시 쓰는 변경입니다. 이런 요청은 반영하지 않습니다. 요청자가
                최신 코드를 받은 뒤 다시 올려야 합니다.
              </p>
            )}
          </div>
        </div>

        <div className="request-col">
          <div className="col-label">승인</div>
          <ApprovalBox
            user={user}
            checks={r.checks}
            approved={approved}
            requester={r.requester}
            requestedAt={fmt.dayClock(r.requestedAt)}
            approveLabel="승인하고 반영"
            onApprove={() => {
              setApproved(true)
              onToast(`${r.workspaceLabel} ${r.commit} 을 승인했습니다. 반영을 시작합니다.`)
            }}
            onReject={() => onToast(`${r.workspaceLabel} ${r.commit} 을 반려했습니다.`)}
            afterNote="승인하면 코드를 받아 봇을 다시 띄우고, 90초 동안 정상 응답을 확인합니다. 확인에 실패하면 직전 상태로 자동으로 되돌립니다. 승인은 10분간 유효하며, 그 사이 새 커밋이 올라오면 다시 승인해야 합니다."
            blockedNote="자동 검사에서 문제가 발견되어 반영할 수 없습니다. 왼쪽 검사 결과를 요청자에게 전달해 주세요."
          />
        </div>
      </div>
    </article>
  )
}

export function Deploy({
  user,
  onToast,
}: {
  user: ConsoleUser
  onToast: (m: string) => void
}) {
  const visible = deployRequests.filter(
    (r) => user.role === 'owner' || user.workspaces.includes(r.workspace),
  )

  return (
    <>
      <PageHead
        crumb="관리 · 배포 승인"
        title="배포 승인"
        note={
          user.role === 'owner'
            ? '워크스페이스 담당자가 봇을 수정해 올리면 이 화면에 요청으로 쌓입니다. 자동 검사를 모두 통과한 요청만 승인 버튼이 열리고, 승인하면 서버에서 코드 받기·재기동·정상 확인까지 자동으로 진행됩니다. 서버에 직접 접속하거나 명령어를 입력할 필요는 없습니다.'
            : '수정한 봇을 올리면 이 화면에 요청으로 남습니다. 자동 검사를 통과한 뒤 관리자가 확인하면 반영되고, 결과는 Slack 으로 알려 드립니다. 반영 전까지 봇은 지금 상태로 계속 동작합니다.'
        }
        aside={
          <>
            <MockBadge />
            <span className="chip flat">
              {user.role === 'owner' ? '승인 권한 있음' : '요청만 가능'}
            </span>
          </>
        }
      />

      <Section
        title="승인 대기 중인 요청"
        note={`${visible.length}건`}
        lead={
          user.role === 'owner'
            ? '검사 항목은 테스트, 코드 형식, 아카이브 문서 형식, 시크릿 유출 네 가지입니다. 하나라도 실패하면 승인할 수 없습니다.'
            : undefined
        }
      >
        {visible.length ? (
          visible.map((r) => <RequestCard key={r.id} r={r} user={user} onToast={onToast} />)
        ) : (
          <Empty
            title="올라온 요청이 없습니다"
            note="봇 코드를 수정해 저장소에 올리면 자동으로 이 목록에 나타납니다."
          />
        )}
      </Section>

      <Section
        title="반영 이력"
        note="추가만 되고 지울 수 없습니다"
        lead="누가 언제 무엇을 승인하고 반영했는지 남습니다. 되돌린 기록도 함께 남습니다."
      >
        <div className="card card-pad">
          <div className="log">
            {deployHistory.map((e) => (
              <div className="log-row" key={e.id}>
                <span className="log-when">{fmt.dayClock(e.at)}</span>
                <span className={`log-action ${ACTION_CLASS[e.action] ?? ''}`}>{e.action}</span>
                <span className="log-text">
                  <b>{e.workspace}</b> <span className="mono">{e.commit}</span> · {e.actor} —{' '}
                  {e.note}
                </span>
              </div>
            ))}
          </div>
        </div>
      </Section>
    </>
  )
}
