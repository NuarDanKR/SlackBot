import { useResource } from '../api/hooks'
import { Chip, Failed, Loading, Metric, PageHead, Section, fmt } from '../components/primitives'
import type { AuditEvent, ConsoleUser, WorkspaceStatus } from '../types'
import { withQuery } from '../navigation'

type Navigate = (path: string) => void

function ActionRow({ title, detail, tone = 'plain', onClick }: {
  title: string; detail: string; tone?: 'plain' | 'watch' | 'bad' | 'ok'; onClick: () => void
}) {
  return (
    <button className="action-row" type="button" onClick={onClick}>
      <span><strong>{title}</strong><small>{detail}</small></span>
      <Chip tone={tone}>{tone === 'ok' ? '정상' : tone === 'plain' ? '보기' : '확인 필요'}</Chip>
    </button>
  )
}

interface CollectionData {
  documents: number
  rawLines: number
  stalled: WorkspaceStatus[]
  brokenDocuments: number
  uninvitedChannels: number
  workspaces: WorkspaceStatus[]
  summaryReview: { available: boolean; pending: number }
}

export function CollectionDashboard({ user, navigate }: { user: ConsoleUser; navigate: Navigate }) {
  const res = useResource<CollectionData>('/api/dashboards/collection')
  if (res.loading) return <Loading what="수집 대시보드를" />
  if (res.error || !res.data) return <Failed what="수집 대시보드를" detail={res.error?.message ?? '응답이 없습니다.'} onRetry={res.reload} />
  const d = res.data
  return <>
    <PageHead crumb="수집" title="수집 대시보드" note="원문이 들어오고 검색 가능한 상태로 유지되는 과정을 한곳에서 확인합니다."
      aside={<Chip tone={d.stalled.length || d.brokenDocuments ? 'watch' : 'ok'}>{d.stalled.length || d.brokenDocuments ? '확인 필요' : '수집 정상'}</Chip>} />
    <Section title="현재 수집량" lead="허용된 워크스페이스 범위만 합산합니다.">
      <div className="metrics overview-metrics"><Metric k="문서" v={fmt.int(d.documents)} unit="건" /><Metric k="원문" v={fmt.int(d.rawLines)} unit="줄" /><Metric k="워크스페이스" v={fmt.int(d.workspaces.length)} unit="개" /></div>
    </Section>
    <Section title="조치할 항목" note={`${d.stalled.length + (d.brokenDocuments ? 1 : 0) + (d.uninvitedChannels ? 1 : 0)}건`}>
      <div className="action-list">
        {d.stalled.map((w) => <ActionRow key={w.key} title={`${w.label} 수집 중단`} detail="최근 수집 시각과 연결 상태를 확인합니다." tone="bad"
          onClick={() => navigate(user.role === 'admin' ? withQuery('/manage/workspaces', { workspace: w.key }) : withQuery('/collect/status', { workspace: w.key, state: 'stalled' }))} />)}
        {d.brokenDocuments > 0 && <ActionRow title={`형식이 깨진 문서 ${fmt.int(d.brokenDocuments)}건`} detail="검색 근거에서 제외될 수 있는 문서입니다." tone="watch" onClick={() => navigate(withQuery('/collect/documents', { state: 'broken' }))} />}
        {d.uninvitedChannels > 0 && <ActionRow title={`미초대 채널 ${fmt.int(d.uninvitedChannels)}개`} detail="Slack 연결·명령 진단에서 워크스페이스별 원인을 확인합니다." tone="watch" onClick={() => navigate('/manage/slack')} />}
        {!d.stalled.length && !d.brokenDocuments && !d.uninvitedChannels && <ActionRow title="조치할 수집 문제가 없습니다" detail="모든 워크스페이스가 정상 범위입니다." tone="ok" onClick={() => navigate('/collect/status')} />}
      </div>
    </Section>
  </>
}

interface AnswerData {
  callsToday: number; spentUsd: number; limitUsd: number
  answers: { groundedRate?: number; errorRate?: number; errors?: number; slowAnswers?: number }
  feedback: { satisfaction?: number | null; openCorrections?: number }
  specialists: { calls: number; success: number; fallback: number }
}

export function AnswerDashboard({ user, navigate }: { user: ConsoleUser; navigate: Navigate }) {
  const res = useResource<AnswerData>('/api/dashboards/answers')
  if (res.loading) return <Loading what="답변 대시보드를" />
  if (res.error || !res.data) return <Failed what="답변 대시보드를" detail={res.error?.message ?? '응답이 없습니다.'} onRetry={res.reload} />
  const d = res.data
  return <>
    <PageHead crumb="답변" title="답변 대시보드" note="근거 확보, 오류, 비용과 사용자 피드백을 함께 봅니다." aside={<Chip tone={(d.answers.errors ?? 0) ? 'watch' : 'ok'}>오늘 질문 {fmt.int(d.callsToday)}건</Chip>} />
    <Section title="오늘의 답변" lead="질문 내용은 표시하지 않고 처리 결과만 집계합니다.">
      <div className="metrics overview-metrics"><Metric k="질문" v={fmt.int(d.callsToday)} unit="건" /><Metric k="근거 확보율" v={d.answers.groundedRate == null ? '-' : `${Math.round(d.answers.groundedRate * 100)}%`} /><Metric k="오류" v={fmt.int(d.answers.errors ?? 0)} unit="건" /><Metric k="사용액" v={fmt.usd(d.spentUsd)} /></div>
    </Section>
    <Section title="분석 바로가기"><div className="action-list">
      <ActionRow title="사용량 및 비용" detail={`하루 상한 ${fmt.usd(d.limitUsd)}`} onClick={() => navigate('/answer/usage')} />
      {user.role !== 'guest' && <ActionRow title="질문 처리 기록" detail={`오류 ${(d.answers.errors ?? 0)}건, 느린 답변 ${(d.answers.slowAnswers ?? 0)}건`} tone={(d.answers.errors ?? 0) ? 'watch' : 'plain'} onClick={() => navigate(withQuery('/answer/questions', { result: (d.answers.errors ?? 0) ? 'error' : null }))} />}
      {user.role !== 'guest' && <ActionRow title="전문 봇 분석" detail={`호출 ${d.specialists.calls}건 · 폴백 ${d.specialists.fallback}건`} tone={d.specialists.fallback ? 'watch' : 'plain'} onClick={() => navigate(withQuery('/answer/specialists', { result: d.specialists.fallback ? 'fallback' : null }))} />}
      {user.role !== 'guest' && <ActionRow title="피드백" detail={`미처리 정정 ${d.feedback.openCorrections ?? 0}건`} tone={(d.feedback.openCorrections ?? 0) ? 'watch' : 'plain'} onClick={() => navigate(withQuery('/answer/feedback', { state: (d.feedback.openCorrections ?? 0) ? 'open' : null }))} />}
    </div></Section>
  </>
}

interface OperationsData {
  slack: { level: 'ok' | 'warn' | 'bad' | 'unknown'; workspaces: { workspace: string; label: string; level: string; connected: boolean | null; problems: string[] }[] }
  commands: { level: string; problems: string[] }
  disabledTimers: number
  deployment: { state: string; message?: string }
  specialistErrors: number
}

export function OperationsDashboard({ user, navigate }: { user: ConsoleUser; navigate: Navigate }) {
  const res = useResource<OperationsData>('/api/dashboards/operations')
  if (res.loading) return <Loading what="운영 대시보드를" />
  if (res.error || !res.data) return <Failed what="운영 대시보드를" detail={res.error?.message ?? '응답이 없습니다.'} onRetry={res.reload} />
  const d = res.data
  const disconnected = d.slack.workspaces.filter((w) => w.connected === false).length
  return <>
    <PageHead crumb="관리" title="운영 대시보드" note="서비스, Slack, 배치, 배포와 전문 봇의 운영 상태를 확인합니다." />
    <Section title="운영 상태"><div className="action-list">
      <ActionRow title="Slack 연결·명령" detail={`연결 끊김 ${disconnected}개 · 명령 문제 ${d.commands.problems.length}건`} tone={disconnected || d.commands.problems.length ? 'bad' : 'ok'} onClick={() => navigate('/manage/slack')} />
      <ActionRow title="전문 봇" detail={`장애 ${d.specialistErrors}개`} tone={d.specialistErrors ? 'bad' : 'plain'} onClick={() => navigate(withQuery('/manage/specialists', { state: d.specialistErrors ? 'error' : null }))} />
      {user.role === 'admin' && <ActionRow title="배치" detail={`사용 중지 ${d.disabledTimers}개`} tone={d.disabledTimers ? 'watch' : 'ok'} onClick={() => navigate(withQuery('/manage/batches', { state: d.disabledTimers ? 'disabled' : null }))} />}
      <ActionRow title="배포" detail={d.deployment.message || d.deployment.state} tone={d.deployment.state === 'failed' ? 'bad' : 'plain'} onClick={() => navigate(withQuery('/manage/deploy', { state: d.deployment.state === 'failed' ? 'failed' : null }))} />
    </div></Section>
  </>
}

interface ConsoleData { users: number; admins: number; pendingApprovals: number; recentAudit: AuditEvent[] }
export function ConsoleDashboard({ navigate }: { navigate: Navigate }) {
  const res = useResource<ConsoleData>('/api/dashboards/console')
  if (res.loading) return <Loading what="콘솔 대시보드를" />
  if (res.error || !res.data) return <Failed what="콘솔 대시보드를" detail={res.error?.message ?? '응답이 없습니다.'} onRetry={res.reload} />
  const d = res.data
  return <>
    <PageHead crumb="콘솔 관리" title="콘솔 대시보드" note="콘솔 접근 권한과 관리 작업의 흔적을 확인합니다." />
    <Section title="접근과 승인"><div className="metrics overview-metrics"><Metric k="사용자" v={fmt.int(d.users)} unit="명" /><Metric k="관리자" v={fmt.int(d.admins)} unit="명" /><Metric k="승인 대기" v={fmt.int(d.pendingApprovals)} unit="건" /></div></Section>
    <Section title="관리 바로가기"><div className="action-list">
      <ActionRow title="콘솔 사용자 관리" detail="계정, 역할과 워크스페이스 범위를 관리합니다." onClick={() => navigate('/console/users')} />
      <ActionRow title="감사 기록" detail={`최근 이벤트 ${d.recentAudit.length}건`} onClick={() => navigate('/console/audit')} />
    </div></Section>
  </>
}
