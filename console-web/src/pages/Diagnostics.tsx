import { useState } from 'react'
import { ApiError, api } from '../api/client'
import { useResource } from '../api/hooks'
import { Chip, Failed, Loading, Metric, PageHead, Section, fmt } from '../components/primitives'
import type { ConsoleUser, HealthLevel, HealthReport } from '../types'

const TONE: Record<HealthLevel, 'ok' | 'watch' | 'bad' | 'plain'> = { ok: 'ok', warn: 'watch', bad: 'bad', unknown: 'plain' }
const LABEL: Record<HealthLevel, string> = { ok: '정상', warn: '확인 필요', bad: '조치 필요', unknown: '판단 보류' }
function Level({ value }: { value: HealthLevel }) { return <Chip tone={TONE[value]}>{LABEL[value]}</Chip> }
function Problems({ items }: { items: string[] }) { return items.length ? <ul className="health-problems">{items.map((item) => <li key={item}>{item}</li>)}</ul> : <p className="hint">확인할 문제가 없습니다.</p> }

export function ArchiveDiagnostics() {
  const res = useResource<{ checkedAt: string; section: HealthReport['sections']['archive'] }>('/api/diagnostics/archive')
  if (res.loading) return <Loading what="아카이브 진단을" />
  if (res.error || !res.data) return <Failed what="아카이브 진단을" detail={res.error?.message ?? '응답이 없습니다.'} onRetry={res.reload} />
  const d = res.data.section
  return <><PageHead crumb="수집 · 아카이브 진단" title="아카이브 진단" note="형식이 깨졌거나 수집이 밀린 원문을 확인합니다." aside={<Level value={d.level} />} />
    <Section title="진단 결과"><div className="metrics overview-metrics"><Metric k="수집 문서" v={fmt.int(d.documents)} unit="건" /><Metric k="깨진 문서" v={fmt.int(d.brokenDocuments)} unit="건" /><Metric k="수집 밀림" v={fmt.int(d.staleWorkspaces)} unit="개" /></div><Problems items={d.problems} /></Section></>
}

export function AnswerQuality() {
  const res = useResource<{ checkedAt: string; section: HealthReport['sections']['answers'] }>('/api/diagnostics/answers')
  if (res.loading) return <Loading what="답변 품질을" />
  if (res.error || !res.data) return <Failed what="답변 품질을" detail={res.error?.message ?? '응답이 없습니다.'} onRetry={res.reload} />
  const d = res.data.section
  return <><PageHead crumb="답변 · 품질" title="답변 품질" note="근거 확보율, 오류와 지연을 질문 본문 없이 분석합니다." aside={<Level value={d.level} />} />
    <Section title="최근 품질"><div className="metrics overview-metrics"><Metric k="질문" v={fmt.int(d.questions)} unit="건" /><Metric k="근거 확보율" v={d.groundedRate == null ? '-' : `${Math.round(d.groundedRate * 100)}%`} /><Metric k="오류" v={fmt.int(d.errors ?? 0)} unit="건" /><Metric k="느린 답변" v={fmt.int(d.slowAnswers ?? 0)} unit="건" /></div><Problems items={d.problems} /></Section></>
}

export function SlackDiagnostics() {
  const res = useResource<{ checkedAt: string; bot: HealthReport['sections']['bot']; commands: HealthReport['sections']['commands'] }>('/api/diagnostics/slack')
  if (res.loading) return <Loading what="Slack 진단을" />
  if (res.error || !res.data) return <Failed what="Slack 진단을" detail={res.error?.message ?? '응답이 없습니다.'} onRetry={res.reload} />
  return <><PageHead crumb="관리 · Slack" title="Slack 연결·명령 진단" note="워크스페이스 연결과 코드·매니페스트의 명령 정합성을 확인합니다." />
    <Section title="워크스페이스 연결" aside={<Level value={res.data.bot.level} />}><div className="table-wrap"><table className="table"><thead><tr><th>워크스페이스</th><th>상태</th><th>연결</th><th>문제</th></tr></thead><tbody>{res.data.bot.workspaces.map((w) => <tr key={w.workspace}><td>{w.label}<div className="ws-key">{w.workspace}</div></td><td><Level value={w.level} /></td><td>{w.connected == null ? '확인 불가' : w.connected ? '연결됨' : '끊김'}</td><td>{w.problems.join(' · ') || '-'}</td></tr>)}</tbody></table></div></Section>
    <Section title="명령 정합성" aside={<Level value={res.data.commands.level} />}><div className="table-wrap"><table className="table"><thead><tr><th>명령</th><th>코드</th><th>매니페스트</th></tr></thead><tbody>{res.data.commands.commands.map((c) => <tr key={c.name}><td className="mono">{c.name}</td><td>{c.inCode ? '등록' : '없음'}</td><td>{c.inManifest ? '등록' : '없음'}</td></tr>)}</tbody></table></div><Problems items={res.data.commands.problems} /></Section></>
}

export function FeedbackPage({ user, onToast }: { user: ConsoleUser; onToast: (message: string) => void }) {
  const res = useResource<{ checkedAt: string; section: HealthReport['sections']['feedback'] }>('/api/feedback')
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  if (res.loading) return <Loading what="피드백을" />
  if (res.error || !res.data) return <Failed what="피드백을" detail={res.error?.message ?? '응답이 없습니다.'} onRetry={res.reload} />
  const d = res.data.section
  async function handle(id: string) {
    const note = window.prompt('처리 내용을 입력하세요. 내용은 감사 기록에 남습니다.') ?? ''
    setBusy(id); setError(null)
    try { await api.put(`/api/health-report/feedback/${id}/handled`, { note }); res.reload(); onToast('피드백을 처리했습니다.') }
    catch (caught) { setError(caught instanceof ApiError ? caught.message : String(caught)) }
    finally { setBusy(null) }
  }
  return <><PageHead crumb="답변 · 피드백" title="피드백" note="반응과 정정 신고를 집계하고 관리자가 처리 상태를 남깁니다." aside={<Level value={d.level} />} />
    {error && <div className="notice bad"><div><div className="notice-title">처리하지 못했습니다.</div><div className="notice-detail">{error}</div></div></div>}
    <Section title="피드백 현황"><div className="metrics overview-metrics"><Metric k="긍정" v={fmt.int(d.positive)} unit="건" /><Metric k="부정" v={fmt.int(d.negative)} unit="건" /><Metric k="근거 없음" v={fmt.int(d.missing)} unit="건" /><Metric k="미처리 정정" v={fmt.int(d.openCorrections)} unit="건" /></div></Section>
    <Section title="정정 및 신고" note={`${d.items.length}건`}><div className="table-wrap"><table className="table"><thead><tr><th>시각</th><th>워크스페이스</th><th>유형</th><th>작성자</th>{user.role === 'admin' && <th>내용</th>}<th>상태</th></tr></thead><tbody>{d.items.map((item) => <tr key={item.id}><td>{fmt.dayClock(item.at)}</td><td>{item.workspace}</td><td>{item.kind}</td><td>{item.name || item.actor}</td>{user.role === 'admin' && <td>{item.text || '-'}</td>}<td>{item.handled ? `처리됨 · ${item.handledBy}` : user.role === 'admin' ? <button className="btn btn-sm" disabled={busy === item.id} onClick={() => handle(item.id)}>처리 표시</button> : '미처리'}</td></tr>)}</tbody></table></div></Section></>
}
