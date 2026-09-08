import type { ConsoleUser, WorkspaceStatus } from '../types'
import { Chip, Metric, fmt, healthChip } from './primitives'

function collectionStatus(workspace: WorkspaceStatus) {
  if (workspace.connected === false) return <Chip tone="bad">Slack 끊김</Chip>
  if (workspace.connected === null) return <Chip tone="watch">연결 미확인</Chip>
  if (!workspace.realtime || workspace.uninvitedChannels > 0) return <Chip tone="watch">확인 필요</Chip>
  return healthChip(workspace.health)
}

function answerStatus(workspace: WorkspaceStatus) {
  if (workspace.answerHealth === 'unknown') return <Chip tone="plain">응답 없음</Chip>
  if (workspace.answerHealth === 'bad') return <Chip tone="bad">오류 {fmt.int(workspace.answerErrorsToday)}건</Chip>
  if (workspace.answerHealth === 'watch') return <Chip tone="watch">확인 필요</Chip>
  return <Chip tone="ok">{fmt.int(workspace.answersToday)}건 정상</Chip>
}

function errorStatus(workspace: WorkspaceStatus) {
  if (workspace.errorHealth === 'unknown') return <Chip tone="plain">판단 자료 없음</Chip>
  if (workspace.errorHealth === 'bad') return <Chip tone="bad">{fmt.int(workspace.answerErrorsToday)}건</Chip>
  return <Chip tone="ok">없음</Chip>
}

export function WorkspaceHealthGrid({ user, workspaces }: { user: ConsoleUser; workspaces: WorkspaceStatus[] }) {
  return <div className="grid grid-3">
    {workspaces.map((workspace) => {
      const encoded = encodeURIComponent(workspace.key)
      const answerHref = user.role === 'guest'
        ? '#/answer'
        : `#/answer/questions?workspace=${encoded}`
      const errorHref = user.role === 'guest'
        ? '#/answer'
        : `#/answer/questions?workspace=${encoded}&result=error`
      return <article className="ws-card workspace-live-card" key={workspace.key}>
        <div className="ws-top">
          <div><div className="ws-name">{workspace.label}</div><div className="ws-key">{workspace.key}{workspace.role === 'root' ? ' · 상위 워크스페이스' : ''}</div></div>
          <a className="text-link" href={user.role === 'admin' ? `#/manage/workspaces?workspace=${encoded}` : `#/collect?workspace=${encoded}`}>상세</a>
        </div>
        <div className="workspace-health-list">
          <a href={`#/collect?workspace=${encoded}`} className="workspace-health-row"><span>수집</span>{collectionStatus(workspace)}</a>
          <a href={answerHref} className="workspace-health-row"><span>답변</span>{answerStatus(workspace)}</a>
          <a href={errorHref} className="workspace-health-row"><span>오류</span>{errorStatus(workspace)}</a>
        </div>
        <div className="metrics">
          <Metric k="문서" v={fmt.int(workspace.docs)} unit="건" />
          <Metric k="오늘 답변" v={fmt.int(workspace.answersToday)} unit="건" />
          <Metric k="채널" v={fmt.int(workspace.channels)} unit="개" />
        </div>
        <div className="workspace-live-times">
          <span>마지막 수집 {workspace.lastIngestedAt ? fmt.dayClock(workspace.lastIngestedAt) : '기록 없음'}</span>
          <span>마지막 답변 {workspace.lastAnsweredAt ? fmt.dayClock(workspace.lastAnsweredAt) : '기록 없음'}</span>
        </div>
        {(workspace.uninvitedChannels > 0 || !workspace.realtime || workspace.brokenDocs > 0) && <p className="hint warn">
          미초대 채널 {fmt.int(workspace.uninvitedChannels)}개 · 실시간 수집 {workspace.realtime ? '사용' : '중지'} · 형식 오류 {fmt.int(workspace.brokenDocs)}건
        </p>}
        {workspace.answerHealth === 'watch' && <p className="hint warn">근거 없음 {fmt.int(workspace.noHitAnswersToday)}건 · 15초 초과 {fmt.int(workspace.slowAnswersToday)}건</p>}
        {workspace.writeProblem && <p className="hint warn">아카이브 저장 오류: {workspace.writeProblem}</p>}
      </article>
    })}
  </div>
}
