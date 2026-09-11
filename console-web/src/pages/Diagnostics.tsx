import { useState } from 'react'
import { ApiError, api } from '../api/client'
import { useResource } from '../api/hooks'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { Chip, Failed, Loading, Metric, MiniBars, PageHead, Section, fmt } from '../components/primitives'
import type { ConsoleUser, HealthLevel, HealthReport } from '../types'

const TONE: Record<HealthLevel, 'ok' | 'watch' | 'bad' | 'plain'> = { ok: 'ok', warn: 'watch', bad: 'bad', unknown: 'plain' }
const LABEL: Record<HealthLevel, string> = { ok: '정상', warn: '확인 필요', bad: '조치 필요', unknown: '판단 보류' }
function Level({ value }: { value: HealthLevel }) { return <Chip tone={TONE[value]}>{LABEL[value]}</Chip> }
function CheckedAt({ value }: { value: string }) { return <span className="updated-at">기준 {new Date(value).toLocaleString('ko-KR')}</span> }
function Problems({ items }: { items: string[] }) { return items.length ? <ul className="health-problems">{items.map((item) => <li key={item}>{item}</li>)}</ul> : <p className="hint">확인할 문제가 없습니다.</p> }

export function ArchiveDiagnostics() {
  type ArchiveSection = HealthReport['sections']['archive'] & {
    attachmentFailures: number
    failedAttachments: {
      workspace: string
      channelId: string
      name: string
      filetype: string
      reason: string
      permalink: string
      stagedAt: string
    }[]
  }
  const res = useResource<{ checkedAt: string; section: ArchiveSection }>('/api/diagnostics/archive')
  if (res.loading) return <Loading what="아카이브 진단을" />
  if (res.error || !res.data) return <Failed what="아카이브 진단을" detail={res.error?.message ?? '응답이 없습니다.'} onRetry={res.reload} />
  const d = res.data.section
  return <><PageHead crumb="수집 · 아카이브 진단" title="아카이브 진단" note="형식이 깨졌거나 수집이 밀린 원문을 확인합니다." aside={<><CheckedAt value={res.data.checkedAt} /><Level value={d.level} /></>} />
    <Section title="진단 결과"><div className="metrics overview-metrics"><Metric k="수집 문서" v={fmt.int(d.documents)} unit="건" /><Metric k="깨진 문서" v={fmt.int(d.brokenDocuments)} unit="건" /><Metric k="첨부 변환 실패" v={fmt.int(d.attachmentFailures)} unit="건" /><Metric k="수집 밀림" v={fmt.int(d.staleWorkspaces)} unit="개" /></div><Problems items={d.problems} /></Section>
    <Section title="첨부 변환 실패" lead="자동 변환에 실패한 첨부입니다. 원본은 격리 보관되며 자동으로 외부 LLM에 전송되지 않습니다.">
      <div className="table-wrap"><table className="table"><thead><tr><th>시각</th><th>워크스페이스</th><th>채널</th><th>파일</th><th>실패 사유</th><th>원본</th></tr></thead><tbody>{d.failedAttachments.length ? d.failedAttachments.map((item, index) => <tr key={`${item.workspace}-${item.channelId}-${item.name}-${index}`}><td>{fmt.dayClock(item.stagedAt)}</td><td className="mono">{item.workspace}</td><td className="mono">{item.channelId}</td><td>{item.name}<div className="subtle mono">{item.filetype || '-'}</div></td><td>{item.reason}</td><td>{item.permalink ? <a href={item.permalink} target="_blank" rel="noreferrer">Slack에서 보기</a> : '-'}</td></tr>) : <tr><td colSpan={6}>변환에 실패한 첨부가 없습니다.</td></tr>}</tbody></table></div>
    </Section>
    <Section title="문서 검사 분포" lead="전체 문서 중 스키마 검사를 통과한 문서와 실패한 문서를 비교합니다.">
      <MiniBars label="문서 검사 결과" items={[{ label: '검사 통과', value: Math.max(0, d.documents - d.brokenDocuments), tone: 'ok' }, { label: '형식 오류', value: d.brokenDocuments, tone: 'bad' }]} />
    </Section>
    <Section title="숫자 산정 근거" lead="숫자가 만들어지는 조건과 실제 원인을 확인할 위치입니다.">
      <ul className="basis-list">
        <li><strong>수집 문서</strong>: 현재 계정이 볼 수 있는 아카이브의 Markdown 문서 수입니다.</li>
        <li><strong>깨진 문서</strong>: 필수 메타데이터 또는 원문 블록 검사에 실패한 문서 수입니다. <a href="#/collect/documents?state=broken">실패 문서와 사유 보기</a></li>
        <li><strong>수집 밀림</strong>: 마지막 수집 시각이 허용 범위를 넘은 워크스페이스 수입니다. <a href="#/collect?state=stalled">해당 워크스페이스 보기</a></li>
      </ul>
    </Section></>
}

type AnswerEvidence = {
  items: {
    at: string
    workspace: string
    question: string
    reason: string
    hits: number
    citations: string[]
    model: string
    elapsedMs: number
    error: string
  }[]
}

const EVIDENCE_REASON: Record<string, string> = {
  error: '오류 발생',
  no_hits: '검색 근거 없음',
  slow: '15초 초과',
}

export function AnswerQuality({ user }: { user: ConsoleUser }) {
  const [showEvidence, setShowEvidence] = useState(false)
  const res = useResource<{ checkedAt: string; section: HealthReport['sections']['answers'] }>('/api/diagnostics/answers')
  const evidence = useResource<AnswerEvidence>(user.role === 'admin' && showEvidence ? '/api/diagnostics/answers/evidence?limit=30' : null, [showEvidence])
  if (res.loading) return <Loading what="답변 품질을" />
  if (res.error || !res.data) return <Failed what="답변 품질을" detail={res.error?.message ?? '응답이 없습니다.'} onRetry={res.reload} />
  const d = res.data.section
  const grounded = d.grounded ?? Math.max(0, d.questions - (d.noHits ?? 0))
  return <><PageHead crumb="답변 · 품질" title="답변 품질" note="최근 7일 질문의 근거 확보, 오류와 지연을 집계합니다." aside={<><CheckedAt value={res.data.checkedAt} /><Level value={d.level} /></>} />
    <Section title="최근 품질"><div className="metrics overview-metrics"><Metric k="질문" v={fmt.int(d.questions)} unit="건" /><Metric k="근거 확보율" v={d.groundedRate == null ? '-' : `${Math.round(d.groundedRate * 100)}%`} /><Metric k="오류" v={fmt.int(d.errors ?? 0)} unit="건" /><Metric k="느린 답변" v={fmt.int(d.slowAnswers ?? 0)} unit="건" /></div><Problems items={d.problems} /></Section>
    <Section title="판정 결과 분포" lead="문제 유형별 건수를 같은 기준으로 비교합니다. 한 질문이 오류·지연 등 여러 항목에 함께 포함될 수 있습니다.">
      <MiniBars label="답변 품질 판정 결과" items={[{ label: '근거 있음', value: grounded, tone: 'ok' }, { label: '근거 없음', value: d.noHits ?? 0, tone: 'warn' }, { label: '오류', value: d.errors ?? 0, tone: 'bad' }, { label: '15초 초과', value: d.slowAnswers ?? 0, tone: 'warn' }]} />
    </Section>
    <Section title="숫자 산정 근거" lead="집계 기간과 판정식을 고정해 같은 숫자를 다시 확인할 수 있습니다.">
      <ul className="basis-list">
        <li><strong>집계 범위</strong>: 현재 계정의 워크스페이스에서 최근 7일 동안 처리한 질문입니다.</li>
        <li><strong>근거 확보율</strong>: 검색 결과가 1건 이상인 질문 ÷ 전체 질문입니다. 표본 10건 미만은 상태 판정을 보류합니다.</li>
        <li><strong>오류</strong>: 처리 기록에 오류 종류가 남은 질문이며, 오류율 2%부터 확인 필요, 10%부터 조치 필요입니다.</li>
        <li><strong>느린 답변</strong>: 총 응답 시간이 15초를 넘은 질문입니다.</li>
      </ul>
      {(d.topReasons?.length ?? 0) > 0 && <div style={{ marginTop: 14 }}><MiniBars label="근거 없음 사유" items={d.topReasons!.map((row) => ({ label: row.reason, value: row.count, tone: 'warn' }))} /></div>}
    </Section>
    <Section title="판정 근거 문장" lead="질문 본문은 업무 내용이 포함될 수 있어 관리자만 명시적으로 열람할 수 있으며, 열람 사실은 감사 기록에 남습니다.">
      {user.role !== 'admin' ? <p className="hint">개발자 화면에는 질문 본문을 표시하지 않습니다. 위 산정식과 질문 처리 기록의 메타데이터로 원인을 확인해 주세요.</p> : !showEvidence ? <button className="btn" type="button" onClick={() => setShowEvidence(true)}>근거 문장 열기</button> : evidence.loading ? <Loading what="판정 근거 문장을" /> : evidence.error ? <Failed what="판정 근거 문장을" detail={evidence.error.message} onRetry={evidence.reload} /> : <div className="evidence-list">{evidence.data?.items.length ? evidence.data.items.map((item, index) => <article className="evidence-item" key={`${item.at}-${index}`}><div><Chip tone={item.reason === 'error' ? 'bad' : 'watch'}>{EVIDENCE_REASON[item.reason] ?? item.reason}</Chip></div><p className="evidence-question">{item.question}</p><div className="evidence-meta">{fmt.dayClock(item.at)} · {item.workspace} · {item.model || '모델 미기록'} · {fmt.ms(item.elapsedMs)}</div><div className="evidence-meta">검색 {item.hits}건 · 출처 {item.citations.join(', ') || '없음'}{item.error ? ` · 오류 ${item.error}` : ''}</div></article>) : <p className="hint">최근 7일 동안 확인할 문제 문장이 없습니다.</p>}</div>}
    </Section></>
}

export function CommandDiagnostics() {
  const res = useResource<{ checkedAt: string; section: HealthReport['sections']['commands'] }>('/api/diagnostics/commands')
  if (res.loading) return <Loading what="명령 진단을" />
  if (res.error || !res.data) return <Failed what="명령 진단을" detail={res.error?.message ?? '응답이 없습니다.'} onRetry={res.reload} />
  const commands = res.data.section
  return <><PageHead crumb="운영 · 명령" title="명령 진단" note="코드에 구현된 Slack 명령과 앱 매니페스트 등록 상태가 일치하는지 확인합니다." aside={<CheckedAt value={res.data.checkedAt} />} />
    <Section title="명령 정합성" aside={<Level value={commands.level} />}><div className="table-wrap"><table className="table"><thead><tr><th>명령</th><th>코드</th><th>매니페스트</th></tr></thead><tbody>{commands.commands.map((c) => <tr key={c.name}><td className="mono">{c.name}</td><td>{c.inCode ? '등록' : '없음'}</td><td>{c.inManifest ? '등록' : '없음'}</td></tr>)}</tbody></table></div><Problems items={commands.problems} /></Section></>
}

export function FeedbackPage({ user, onToast }: { user: ConsoleUser; onToast: (message: string) => void }) {
  const res = useResource<{ checkedAt: string; section: HealthReport['sections']['feedback'] }>('/api/feedback')
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [handling, setHandling] = useState<{ id: string; note: string } | null>(null)
  if (res.loading) return <Loading what="피드백을" />
  if (res.error || !res.data) return <Failed what="피드백을" detail={res.error?.message ?? '응답이 없습니다.'} onRetry={res.reload} />
  const d = res.data.section
  async function handle(id: string, note: string) {
    setBusy(id); setError(null)
    try { await api.put(`/api/health-report/feedback/${id}/handled`, { note }); res.reload(); onToast('피드백을 처리했습니다.') }
    catch (caught) { setError(caught instanceof ApiError ? caught.message : String(caught)) }
    finally { setBusy(null) }
  }
  return <><PageHead crumb="답변 · 피드백" title="피드백" note="반응과 정정 신고를 집계하고 관리자가 처리 상태를 남깁니다." aside={<><CheckedAt value={res.data.checkedAt} /><Level value={d.level} /></>} />
    {error && <div className="notice bad"><div><div className="notice-title">처리하지 못했습니다.</div><div className="notice-detail">{error}</div></div></div>}
    <Section title="피드백 현황"><div className="metrics overview-metrics"><Metric k="긍정" v={fmt.int(d.positive)} unit="건" /><Metric k="부정" v={fmt.int(d.negative)} unit="건" /><Metric k="근거 없음" v={fmt.int(d.missing)} unit="건" /><Metric k="미처리 정정" v={fmt.int(d.openCorrections)} unit="건" /></div></Section>
    <Section title="정정 및 신고" note={`${d.items.length}건`}><div className="table-wrap"><table className="table"><thead><tr><th>시각</th><th>워크스페이스</th><th>유형</th><th>작성자</th>{user.role === 'admin' && <th>내용</th>}<th>상태</th></tr></thead><tbody>{d.items.map((item) => <tr key={item.id}><td>{fmt.dayClock(item.at)}</td><td>{item.workspace}</td><td>{item.kind}</td><td>{item.name || item.actor}</td>{user.role === 'admin' && <td>{item.text || '-'}</td>}<td>{item.handled ? `처리됨 · ${item.handledBy}` : user.role === 'admin' ? <button className="btn btn-sm" disabled={busy === item.id} onClick={() => setHandling({ id: item.id, note: '' })}>처리 표시</button> : '미처리'}</td></tr>)}</tbody></table></div></Section>
    <ConfirmDialog open={handling !== null} title="이 피드백을 처리 완료로 표시할까요?"
      detail="처리 내용과 실행자는 감사 기록에 남습니다. 실제 확인이나 정정이 끝난 항목만 처리해 주세요."
      confirmLabel="처리 완료" busy={busy !== null} onCancel={() => setHandling(null)}
      onConfirm={() => { if (handling) void handle(handling.id, handling.note).finally(() => setHandling(null)) }}>
      <div className="field"><label className="field-label" htmlFor="feedback-note">처리 내용</label><textarea id="feedback-note" className="input dialog-textarea" value={handling?.note ?? ''} placeholder="무엇을 확인하거나 정정했는지 입력" onChange={(event) => handling && setHandling({ ...handling, note: event.target.value })} /></div>
    </ConfirmDialog>
  </>
}
