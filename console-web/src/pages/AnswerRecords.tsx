import { useEffect, useState } from 'react'
import { useResource } from '../api/hooks'
import { DateRangeFilter, periodLabel } from '../components/DateRangeFilter'
import { Chip, Empty, Failed, Loading, PageHead, Section, fmt } from '../components/primitives'
import { withQuery } from '../navigation'
import type { AnswerRecordDetail, AnswerRecordsResponse, ConsoleUser } from '../types'

const RESULT_LABEL: Record<string, string> = {
  answered: '답변 완료', no_hits: '근거 없음', no_access: '열람 근거 없음',
  error: '오류', blocked: '권한 차단', slow: '15초 초과',
}
const FEEDBACK_LABEL: Record<string, string> = {
  positive: '정확했다', negative: '틀렸다', missing: '근거를 못 찾았다', correction: '정정',
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
            <span className="answer-record-title">{row.asker} · {RESULT_LABEL[row.reason] ?? row.reason}</span>
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
  </>
}

function RecordDetail({ item }: { item: AnswerRecordDetail }) {
  return <article className="answer-original">
    <div className="answer-original-head"><div><strong>{item.asker}</strong><div className="answer-record-meta">{item.userId || '사용자 ID 미기록'} · {item.workspace} · {item.channel || item.channelId || '채널 미기록'}</div></div><Chip tone={item.error ? 'bad' : item.qualityReasons.length ? 'watch' : 'ok'}>{item.error ? '오류' : item.qualityReasons.length ? '확인 필요' : '답변 완료'}</Chip></div>
    <div className="original-block"><div className="original-label">질문 원본 · {item.at ? new Date(item.at).toLocaleString('ko-KR') : '-'}</div><div className="original-text">{item.question || '질문 본문이 기록되지 않았습니다.'}</div></div>
    <div className="original-block"><div className="original-label">전달된 답변</div><div className="original-text">{item.answer || '답변 본문이 기록되지 않았습니다.'}</div></div>
    <dl className="answer-trace"><div><dt>분류·라우팅</dt><dd>{item.intent || '-'} · {item.source || '-'}</dd></div><div><dt>권한·근거</dt><dd>{item.scope || '범위 미기록'} · 검색 {item.hits}건</dd></div><div><dt>모델 실행</dt><dd>{item.model} · {fmt.ms(item.ms)} · {item.costUsd ? fmt.usd(item.costUsd) : '-'}</dd></div><div><dt>전달</dt><dd>{item.responseTs || '응답 시각 미기록'}</dd></div></dl>
    <div className="original-block"><div className="original-label">출처</div>{item.citations.length ? <ul className="citation-list">{item.citations.map((citation, index) => <li key={`${citation}-${index}`}>{citation}</li>)}</ul> : <p className="hint">기록된 출처가 없습니다.</p>}</div>
    <div className="original-block"><div className="original-label">사람 검토·피드백</div>{item.feedback.length ? <div className="feedback-detail-list">{item.feedback.map((feedback) => <div className="feedback-detail" key={feedback.id}><strong>{FEEDBACK_LABEL[feedback.kind] ?? feedback.kind}</strong><span>{feedback.at ? new Date(feedback.at).toLocaleString('ko-KR') : ''} · {feedback.actor}</span>{feedback.text && <p>{feedback.text}</p>}{feedback.handled && <p className="hint">처리됨 · {feedback.handledBy}{feedback.handledNote ? ` · ${feedback.handledNote}` : ''}</p>}</div>)}</div> : <p className="hint">연결된 피드백이 없습니다.</p>}</div>
  </article>
}
