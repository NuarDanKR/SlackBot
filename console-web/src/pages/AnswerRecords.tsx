import { useEffect, useState } from 'react'
import { ApiError, api } from '../api/client'
import { useResource } from '../api/hooks'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { DateRangeFilter, periodLabel } from '../components/DateRangeFilter'
import { Chip, Empty, Failed, Loading, PageHead, Section, fmt } from '../components/primitives'
import { withQuery } from '../navigation'
import type { AnswerRecordDetail, AnswerRecordsResponse, ConsoleUser } from '../types'

const FEEDBACK_LABEL: Record<string, string> = {
  positive: '정확했다', negative: '틀렸다', missing: '근거를 못 찾았다', correction: '정정',
}
const STATUS_LABEL = { answered: '답변 완료', needs_review: '확인 필요', failed: '답변 실패' } as const
const STATUS_TONE = { answered: 'ok', needs_review: 'watch', failed: 'bad' } as const
// 근거를 찾은 경로(B-93). 코드는 서버 `QARecord.search_path` 와 같다. 모르는 코드는 그대로 보여 준다.
const SEARCH_PATH_LABEL: Record<string, string> = {
  exhaustive: '전수',
  index: '색인',
  scan_no_index: '파일 훑음(색인 읽기 실패)',
  scan_index_empty: '파일 재훑음(색인 0건)',
}
// 빈 목록은 「검색을 안 했다」가 아니라 이 칸이 생기기 전 기록이다.
function searchPathText(path: string[], stats: { visibleLines: number; ms: number }[] = []): string {
  if (path.length === 0) return '기록 없음'
  // 숫자는 경로와 같은 순서로 짝지어 붙는다. 숫자가 없는 옛 기록은 이름만 보인다.
  return path.map((code, i) => {
    const name = SEARCH_PATH_LABEL[code] ?? code
    const stat = stats[i]
    return stat ? `${name}(${linesText(stat.visibleLines)} · ${fmt.ms(stat.ms)})` : name
  }).join(' → ')
}
// 요약 질문은 검색이 아니라 기간 스캔이라 경로 대신 이 줄을 쓴다(B-93). 「읽은 줄 → 기간 안 줄 →
// 쓴 줄」 로 어디서 줄어드는지 보이고, 채널당 상한(기본 60줄)에 걸려 잘린 줄이 있으면 따로 말한다.
function summaryStatsText(stats: AnswerRecordDetail['summaryStats']): string {
  if (stats.length === 0) return ''
  return stats.map((s) => {
    const cut = s.windowLines - s.usedLines
    const head = `읽은 ${linesText(s.visibleLines)} → 기간 안 ${linesText(s.windowLines)} → 쓴 ${linesText(s.usedLines)}`
    const tail = cut > 0 ? ` · 상한에 ${linesText(cut)} 잘림(채널 ${s.cappedChannels}/${s.channels}개)` : ' · 잘린 줄 없음'
    return `${head}${tail} · ${fmt.ms(s.ms)}`
  }).join(' / ')
}
// 「권한·근거」 칸 끝에 붙는 문장. 요약은 검색 경로가 없으니 「경로: 기록 없음」 대신 스캔 숫자를 보인다.
function scanText(item: AnswerRecordDetail): string {
  const path = item.searchPath ?? []
  const summary = summaryStatsText(item.summaryStats ?? [])
  const parts: string[] = []
  if (path.length > 0 || !summary) parts.push(`경로: ${searchPathText(path, item.searchStats ?? [])}`)
  if (summary) parts.push(`요약 스캔: ${summary}`)
  return parts.join(' · ')
}
// 줄 수: 1만 줄부터는 「12.4만 줄」, 그 아래는 「8,200줄」. 15만 줄 문턱과 견주어 읽기 쉽게 한다.
function linesText(lines: number): string {
  return lines >= 10000 ? `${(lines / 10000).toFixed(1)}만 줄` : `${fmt.int(lines)}줄`
}

export function AnswerRecords({
  user,
  query,
  navigate,
}: {
  user: ConsoleUser
  query: URLSearchParams
  navigate: (path: string) => void
}) {
  const start = query.get('start') ?? ''
  const end = query.get('end') ?? ''
  const workspace = query.get('workspace') ?? ''
  const result = query.get('result') ?? ''
  const asker = query.get('asker') ?? ''
  const selected = query.get('record') ?? ''
  const [workspaceDraft, setWorkspaceDraft] = useState(workspace)
  const [resultDraft, setResultDraft] = useState(result)
  const [askerDraft, setAskerDraft] = useState(asker)
  useEffect(() => {
    setWorkspaceDraft(workspace); setResultDraft(result); setAskerDraft(asker)
  }, [workspace, result, asker])

  const params = new URLSearchParams({
    ...(start && { start }), ...(end && { end }), ...(workspace && { workspace }),
    ...(result && { result }), ...(asker && { asker }),
  })
  const records = useResource<AnswerRecordsResponse>(`/api/answer-records?${params}`)
  const detail = useResource<AnswerRecordDetail>(selected ? `/api/answer-records/${selected}` : null, [selected])

  if (records.loading && !records.data) return <Loading what="질문·답변 원본 목록을" />
  if (records.error || !records.data) return <Failed what="질문·답변 원본 목록을" detail={records.error?.message ?? '응답이 없습니다.'} onRetry={records.reload} />
  const data = records.data

  function move(values: Record<string, string | null>) {
    navigate(withQuery('/answer/records', {
      start: data.periodStart, end: data.periodEnd, workspace, result, asker, ...values,
    }))
  }

  return <>
    <PageHead crumb="답변 · 질문·답변 원본" title="질문·답변 원본"
      note="실제 질문과 Slack에 전달된 답변을 확인합니다. 이 기록은 감사 자료이며 다음 답변의 근거로 사용되지 않습니다."
      aside={<Chip tone={data.view === 'all' ? 'brand' : 'info'}>{data.view === 'all' ? '관리자 전체 범위' : '본인·검토 채널 범위'}</Chip>} />

    {!data.identityLinked && user.role !== 'admin' && <div className="notice warn"><div><div className="notice-title">Slack 사용자 연결을 확인할 수 없습니다</div><div className="notice-detail">회사 이메일과 Slack 사용자 매핑이 확인되면 본인 질문과 담당 검토 채널 기록이 표시됩니다. 이름으로 추측해 열어 주지는 않습니다.</div></div></div>}

    <Section title="조회 기간" note={periodLabel(data.periodStart, data.periodEnd)}>
      <DateRangeFilter path="/answer/records" query={query} navigate={navigate} defaultStart={data.periodStart} defaultEnd={data.periodEnd} today={data.today} preserve={{ workspace, result, asker }} />
    </Section>
    <Section title="원본 찾기" note={`${data.records.length}건`}>
      <form className="filter-row" onSubmit={(event) => { event.preventDefault(); navigate(withQuery('/answer/records', { start: data.periodStart, end: data.periodEnd, workspace: workspaceDraft.trim(), result: resultDraft, asker: askerDraft.trim() })) }}>
        <div className="field"><label className="field-label" htmlFor="record-workspace">워크스페이스</label><input id="record-workspace" className="input" value={workspaceDraft} placeholder="예: tyit" onChange={(event) => setWorkspaceDraft(event.target.value)} /></div>
        <div className="field"><label className="field-label" htmlFor="record-result">처리·품질 결과</label><select id="record-result" className="input" value={resultDraft} onChange={(event) => setResultDraft(event.target.value)}><option value="">모든 결과</option><option value="answered">답변 완료</option><option value="no_hits">근거 없음</option><option value="error">오류</option><option value="slow">15초 초과</option><option value="feedback">피드백 있음</option></select></div>
        {user.role === 'admin' && <div className="field"><label className="field-label" htmlFor="record-asker">질문자</label><input id="record-asker" className="input" value={askerDraft} placeholder="이름 또는 Slack ID" onChange={(event) => setAskerDraft(event.target.value)} /></div>}
        <div className="filter-actions"><button className="btn btn-primary btn-sm" type="submit">조회</button>{(workspace || result || asker) && <button className="btn btn-sm btn-quiet" type="button" onClick={() => navigate(withQuery('/answer/records', { start: data.periodStart, end: data.periodEnd }))}>초기화</button>}</div>
      </form>

      <div className="answer-browser">
        <div className="answer-record-list" aria-label="질문·답변 기록">
          {data.records.map((row) => <button key={row.recordKey} type="button" className={`answer-record-item ${selected === row.recordKey ? 'is-active' : ''}`} onClick={() => move({ record: row.recordKey })}>
            <span className="answer-record-title">{row.asker} · {STATUS_LABEL[row.resultStatus]}</span>
            <span className="answer-record-meta">{fmt.dayClock(row.at)} · {row.workspace} · {row.channel || '채널 미기록'}</span>
            <span className="answer-record-meta">{row.model} · 근거 {row.hits}건{row.hasFeedback ? ' · 피드백 있음' : ''}</span>
          </button>)}
          {!data.records.length && <Empty title="표시할 질문·답변이 없습니다" note="기간과 필터 또는 본인·검토 채널 권한을 확인해 주세요." />}
        </div>

        <div className="answer-record-detail">
          {!selected ? <Empty title="기록을 선택해 주세요" note="목록에서 한 건을 선택할 때만 질문과 답변 원본을 불러오고 열람 기록을 남깁니다." /> : detail.loading ? <Loading what="질문·답변 원본을" /> : detail.error || !detail.data ? <Failed what="질문·답변 원본을" detail={detail.error?.message ?? '응답이 없습니다.'} onRetry={detail.reload} /> : <RecordDetail item={detail.data} />}
        </div>
      </div>
    </Section>

    {user.role === 'admin' && (workspace
      ? <FactCorrectionsSection key={workspace} workspace={workspace} />
      : <Section title="사람이 남긴 정정" note="워크스페이스를 지정하면 보입니다"><p className="hint">위 「원본 찾기」 에서 워크스페이스를 정하고 조회하면 그 워크스페이스의 정정 목록과 철회 버튼이 나옵니다.</p></Section>)}
  </>
}

interface FactCorrectionRow {
  id: number
  correctorId: string
  correctorName: string
  subject: string
  before: string
  after: string
  body: string
  channelId: string
  createdAt: string | null
  expiresAt: string | null
  showCount: number
  revokedAt: string | null
  revokedBy: string
  revokeReason: string
}

/** 사람이 남긴 사실 정정. 원문 권한자 모두의 답변에 이름과 함께 붙으므로 관리자가 철회할 수 있어야 한다. */
function FactCorrectionsSection({ workspace }: { workspace: string }) {
  const list = useResource<{ workspace: string; corrections: FactCorrectionRow[] }>(`/api/fact-corrections?workspace=${encodeURIComponent(workspace)}`, [workspace])
  const [target, setTarget] = useState<FactCorrectionRow | null>(null)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function revoke() {
    if (!target) return
    setBusy(true)
    setError('')
    try {
      await api.post(`/api/fact-corrections/${target.id}/revoke`, { workspace, reason: reason.trim() })
      setTarget(null)
      setReason('')
      list.reload()
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught))
    } finally {
      setBusy(false)
    }
  }

  const note = '원문을 지금 볼 수 있는 사람의 관련 답변 끝에 「정정자 · 날짜 · 전 → 후」 로 붙습니다. 원문은 바뀌지 않습니다.'
  if (list.loading && !list.data) return <Section title="사람이 남긴 정정" note={note}><Loading what="정정 목록을" /></Section>
  if (list.error || !list.data) return <Section title="사람이 남긴 정정" note={note}><Failed what="정정 목록을" detail={list.error?.message ?? '응답이 없습니다.'} onRetry={list.reload} /></Section>
  const rows = list.data.corrections
  return <Section title="사람이 남긴 정정" note={note}>
    {error && <div className="notice bad"><div><div className="notice-title">철회하지 못했습니다</div><div className="notice-detail">{error}</div></div></div>}
    {rows.length === 0 ? <Empty title="남겨진 정정이 없습니다" note="Slack 에서 「15억이 아니라 12억」 처럼 무엇에서 무엇으로 바뀌었는지 말하면 생깁니다." /> :
      <div className="table-wrap"><table className="table">
        <thead><tr><th>상태</th><th>정정자 · 날짜</th><th>무엇을</th><th>전 → 후</th><th>표시</th><th></th></tr></thead>
        <tbody>{rows.map((row) => <tr key={row.id}>
          <td>{row.revokedAt ? <Chip tone="plain">철회</Chip> : <Chip tone="ok">표시 중</Chip>}</td>
          <td>{row.correctorName || row.correctorId}<div className="hint">{row.createdAt ? fmt.dayClock(row.createdAt) : '-'}</div></td>
          <td>{row.subject || '-'}<div className="hint">{row.body}</div></td>
          <td className="mono">{row.before} → {row.after}</td>
          <td>{row.showCount}회{row.revokedAt && <div className="hint">{row.revokedBy} · {row.revokeReason}</div>}</td>
          <td>{!row.revokedAt && <button className="btn btn-sm btn-quiet" type="button" onClick={() => { setTarget(row); setReason(''); setError('') }}>철회</button>}</td>
        </tr>)}</tbody>
      </table></div>}
    <ConfirmDialog open={target !== null}
      title="이 정정을 철회할까요?"
      detail={target ? `${target.correctorName || target.correctorId} 님의 정정 「${target.before} → ${target.after}」 이 다음 답변부터 표시되지 않습니다. 기록은 지우지 않고 실행자·사유를 남깁니다.` : ''}
      confirmLabel="철회" busy={busy || !reason.trim()}
      onCancel={() => setTarget(null)}
      onConfirm={() => { void revoke() }}>
      <div className="field"><label className="field-label" htmlFor="fact-correction-reason">철회 사유 (감사 기록, 필수)</label>
        <input id="fact-correction-reason" className="input" maxLength={300} value={reason} onChange={(event) => setReason(event.target.value)} /></div>
    </ConfirmDialog>
  </Section>
}

function RecordDetail({ item }: { item: AnswerRecordDetail }) {
  return <article className="answer-original">
    <div className="answer-original-head"><div><strong>{item.asker}</strong><div className="answer-record-meta">{item.userId || '사용자 ID 미기록'} · {item.workspace} · {item.channel || item.channelId || '채널 미기록'}</div></div><Chip tone={STATUS_TONE[item.resultStatus]}>{STATUS_LABEL[item.resultStatus]}</Chip></div>
    {item.resultStatus === 'failed' && <div className="notice bad"><div><div className="notice-title">업무 답변을 생성하지 못했습니다</div><div className="notice-detail">Slack 전달 여부와 별개인 답변 생성 실패입니다. 사유 코드: <span className="mono">{item.failureCode || item.specialistErrorCode || 'answer_failed'}</span></div></div></div>}
    <div className="original-block"><div className="original-label">질문 원본 · {item.at ? new Date(item.at).toLocaleString('ko-KR') : '-'}</div><div className="original-text">{item.question || '질문 본문이 기록되지 않았습니다.'}</div></div>
    <div className="original-block"><div className="original-label">전달된 답변</div><div className="original-text">{item.answer || '답변 본문이 기록되지 않았습니다.'}</div></div>
    <dl className="answer-trace"><div><dt>분류·라우팅</dt><dd>{item.intent || '-'} · {item.source || '-'}{item.plannerModel ? ` · ${item.plannerModel}` : ''}</dd></div><div><dt>권한·근거</dt><dd>{item.scope || '범위 미기록'} · 검색 {item.hits}건 · {scanText(item)}</dd></div><div><dt>모델 실행</dt><dd>{item.model} · {fmt.ms(item.ms)} · {item.costUsd ? fmt.usd(item.costUsd) : '-'}</dd></div><div><dt>전달</dt><dd>{item.responseTs || '응답 시각 미기록'}</dd></div></dl>
    {item.taskTraces.length > 0 && <div className="original-block"><div className="original-label">작업별 처리</div><div className="table-wrap"><table className="table"><thead><tr><th>작업</th><th>요구 능력</th><th>신뢰도</th><th>응답 주체</th><th>결과</th></tr></thead><tbody>{item.taskTraces.map((trace) => <tr key={`${item.decisionId}-${trace.task_index}`}><td className="mono">{trace.task_kind}</td><td className="mono">{trace.required_capability || '-'}</td><td>{Math.round(trace.routing_confidence * 100)}%</td><td className="mono">{trace.final_responder || 'none'}{trace.attempted_specialists.length > 0 && <div className="hint">시도: {trace.attempted_specialists.join(', ')}</div>}</td><td>{trace.result || '-'}{trace.error_code && <div className="hint mono">{trace.error_code}</div>}</td></tr>)}</tbody></table></div></div>}
    <div className="original-block"><div className="original-label">출처</div>{item.citations.length ? <ul className="citation-list">{item.citations.map((citation, index) => <li key={`${citation}-${index}`}>{citation}</li>)}</ul> : <p className="hint">기록된 출처가 없습니다.</p>}</div>
    <div className="original-block"><div className="original-label">사람 검토·피드백</div>{item.feedback.length ? <div className="feedback-detail-list">{item.feedback.map((feedback) => <div className="feedback-detail" key={feedback.id}><strong>{FEEDBACK_LABEL[feedback.kind] ?? feedback.kind}</strong><span>{feedback.at ? new Date(feedback.at).toLocaleString('ko-KR') : ''} · {feedback.actor}</span>{feedback.text && <p>{feedback.text}</p>}{feedback.handled && <p className="hint">처리됨 · {feedback.handledBy}{feedback.handledNote ? ` · ${feedback.handledNote}` : ''}</p>}</div>)}</div> : <p className="hint">연결된 피드백이 없습니다.</p>}</div>
  </article>
}
