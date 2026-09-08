import { useEffect } from 'react'
import { useResource } from '../api/hooks'
import { Chip, Failed, Loading, Metric, PageHead, Section, fmt } from '../components/primitives'
import { WorkspaceHealthGrid } from '../components/WorkspaceHealthGrid'
import type { ConsoleUser, WorkspaceStatus } from '../types'

interface CollectionData {
  documents: number
  rawLines: number
  stalled: WorkspaceStatus[]
  brokenDocuments: number
  uninvitedChannels: number
  workspaces: WorkspaceStatus[]
}

interface AnswerData {
  callsToday: number
  spentUsd: number
  limitUsd: number
  answers: { errors?: number; slowAnswers?: number }
  feedback: { openCorrections?: number }
}

interface OperationsData {
  commands: { problems: string[] }
  disabledTimers: number
  deployment: { state: string; message?: string }
  specialistErrors: number
}

interface ConsoleData { users: number; admins: number; pendingApprovals: number }

function HomeAction({ href, title, detail, tone = 'plain' }: {
  href: string
  title: string
  detail: string
  tone?: 'plain' | 'watch' | 'bad' | 'ok'
}) {
  return <a className="action-row action-link" href={`#${href}`}>
    <span><strong>{title}</strong><small>{detail}</small></span>
    <Chip tone={tone}>{tone === 'ok' ? '정상' : tone === 'plain' ? '보기' : '확인 필요'}</Chip>
  </a>
}

export function Home({ user }: { user: ConsoleUser }) {
  const collection = useResource<CollectionData>('/api/dashboards/collection')
  const answer = useResource<AnswerData>('/api/dashboards/answers')
  const operations = useResource<OperationsData>(user.role === 'guest' ? null : '/api/dashboards/operations')
  const consoleData = useResource<ConsoleData>(user.role === 'admin' ? '/api/dashboards/console' : null)
  useEffect(() => {
    const timer = window.setInterval(() => {
      collection.reload()
      answer.reload()
      operations.reload()
      consoleData.reload()
    }, 30_000)
    return () => window.clearInterval(timer)
  }, [answer.reload, collection.reload, consoleData.reload, operations.reload])
  const firstLoad = collection.loading || answer.loading || (user.role !== 'guest' && operations.loading) || (user.role === 'admin' && consoleData.loading)
  const firstError = collection.error ?? answer.error ?? operations.error ?? consoleData.error

  if (firstLoad && !collection.data && !answer.data) return <Loading what="오늘의 현황을" />
  if (firstError && !collection.data && !answer.data) {
    return <Failed what="오늘의 현황을" detail={firstError.message} onRetry={() => {
      collection.reload(); answer.reload(); operations.reload(); consoleData.reload()
    }} />
  }

  const c = collection.data
  const a = answer.data
  const o = operations.data
  const admin = consoleData.data
  const disconnected = c?.workspaces.filter((item) => item.connected === false).length ?? 0
  const collectionIssues = (c?.stalled.length ?? 0) + (c?.brokenDocuments ? 1 : 0) + (c?.uninvitedChannels ? 1 : 0)
  const answerIssues = (a?.answers.errors ?? 0) + (a?.feedback.openCorrections ?? 0)
  const operationIssues = disconnected + (o?.commands.problems.length ?? 0) + (o?.specialistErrors ?? 0)
  const issueAreas = Number(collectionIssues > 0) + Number(answerIssues > 0) + Number(operationIssues > 0) + Number((admin?.pendingApprovals ?? 0) > 0)

  return <>
    <PageHead crumb="홈" title="오늘의 운영 현황"
      note="수집, 답변, 운영과 승인 상태에서 지금 확인할 항목을 먼저 보여줍니다."
      aside={<><Chip tone={issueAreas ? 'watch' : 'ok'}>{issueAreas ? `확인할 영역 ${issueAreas}개` : '모두 정상'}</Chip>
        <button className="btn btn-sm" type="button" onClick={() => {
          collection.reload(); answer.reload(); operations.reload(); consoleData.reload()
        }}>새로고침</button></>} />

    <Section title="오늘의 핵심 지표" lead="현재 로그인한 계정에 허용된 워크스페이스만 합산합니다.">
      <div className="metrics overview-metrics home-metrics">
        <Metric k="수집 문서" v={c ? fmt.int(c.documents) : '-'} unit={c ? '건' : undefined} />
        <Metric k="오늘 질문" v={a ? fmt.int(a.callsToday) : '-'} unit={a ? '건' : undefined} />
        <Metric k="오늘 사용액" v={a ? fmt.usd(a.spentUsd) : '-'} />
        {user.role === 'admin' && <Metric k="승인 대기" v={admin ? fmt.int(admin.pendingApprovals) : '-'} unit={admin ? '건' : undefined} />}
      </div>
    </Section>

    <Section title="워크스페이스 실시간 상태" note={`${c?.workspaces.length ?? 0}개 · 30초마다 갱신`}
      lead="워크스페이스별 수집, 오늘 답변과 답변 처리 오류 상태를 함께 확인합니다. 상태를 누르면 원인 화면으로 이동합니다.">
      {c?.workspaces.length
        ? <WorkspaceHealthGrid user={user} workspaces={c.workspaces} />
        : <p className="hint">표시할 워크스페이스가 없습니다.</p>}
    </Section>

    <Section title="지금 조치할 항목" note={issueAreas ? `${issueAreas}개 영역 확인 필요` : '확인할 문제 없음'}>
      <div className="action-list">
        <HomeAction href="/collect" title="수집 상태" detail={`중단 ${c?.stalled.length ?? 0}개 · 형식 오류 문서 ${c?.brokenDocuments ?? 0}건`} tone={collectionIssues ? 'bad' : 'ok'} />
        <HomeAction href={user.role === 'guest' ? '/answer' : '/answer/quality'} title={user.role === 'guest' ? '답변 현황' : '답변 품질'} detail={`오류 ${a?.answers.errors ?? 0}건 · 느린 답변 ${a?.answers.slowAnswers ?? 0}건`} tone={answerIssues ? 'watch' : 'ok'} />
        {user.role !== 'guest' && <HomeAction href="/manage/commands" title="서비스와 명령" detail={`연결 끊김 ${disconnected}개 · 명령 문제 ${o?.commands.problems.length ?? 0}건`} tone={operationIssues ? 'bad' : 'ok'} />}
        {user.role === 'admin' && <HomeAction href="/console/audit" title="승인과 감사" detail={`승인 대기 ${admin?.pendingApprovals ?? 0}건 · 사용자 ${admin?.users ?? 0}명`} tone={admin?.pendingApprovals ? 'watch' : 'plain'} />}
      </div>
    </Section>
  </>
}
