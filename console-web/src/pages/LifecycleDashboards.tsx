import { useResource } from '../api/hooks'
import { Chip, Failed, Loading, Metric, PageHead, Section, fmt } from '../components/primitives'
import type { AuditEvent, ConsoleUser } from '../types'
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
    <PageHead crumb="답변" title="답변 개요" note="근거 확보, 오류, 비용과 사용자 피드백을 함께 봅니다." aside={<Chip tone={(d.answers.errors ?? 0) ? 'watch' : 'ok'}>오늘 질문 {fmt.int(d.callsToday)}건</Chip>} />
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
    <PageHead crumb="운영" title="운영 현황" note="서비스, Slack, 배치, 배포와 전문 봇의 운영 상태를 확인합니다." />
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
    <PageHead crumb="설정·권한" title="권한 현황" note="콘솔 접근 권한과 관리 작업의 흔적을 확인합니다." />
    <Section title="접근과 승인"><div className="metrics overview-metrics"><Metric k="사용자" v={fmt.int(d.users)} unit="명" /><Metric k="관리자" v={fmt.int(d.admins)} unit="명" /><Metric k="승인 대기" v={fmt.int(d.pendingApprovals)} unit="건" /></div></Section>
    <Section title="관리 바로가기"><div className="action-list">
      <ActionRow title="콘솔 사용자 관리" detail="계정, 역할과 워크스페이스 범위를 관리합니다." onClick={() => navigate('/console/users')} />
      <ActionRow title="감사 기록" detail={`최근 이벤트 ${d.recentAudit.length}건`} onClick={() => navigate('/console/audit')} />
    </div></Section>
  </>
}
