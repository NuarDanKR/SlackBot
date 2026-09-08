import { useEffect, useState } from 'react'
import { useResource } from '../api/hooks'
import { Failed, Loading, PageHead, Section, fmt } from '../components/primitives'
import { DateRangeFilter, periodLabel } from '../components/DateRangeFilter'
import type { CallRow } from '../types'
import { withQuery } from '../navigation'

const RESULT_LABEL: Record<string, string> = {
  answered: '답변 완료', no_hits: '근거 없음', error: '오류', blocked: '권한 차단',
}

export function Questions({ query, navigate }: { query: URLSearchParams; navigate: (path: string) => void }) {
  const workspace = query.get('workspace') ?? ''
  const result = query.get('result') ?? ''
  const start = query.get('start') ?? ''
  const end = query.get('end') ?? ''
  const [workspaceDraft, setWorkspaceDraft] = useState(workspace)
  const [resultDraft, setResultDraft] = useState(result)
  useEffect(() => { setWorkspaceDraft(workspace); setResultDraft(result) }, [workspace, result])
  const path = `/api/questions?${new URLSearchParams({ ...(workspace && { workspace }), ...(result && { result }), ...(start && { start }), ...(end && { end }) }).toString()}`
  const res = useResource<{ today: string; periodStart: string; periodEnd: string; questions: CallRow[] }>(path)
  if (res.loading) return <Loading what="질문 처리 기록을" />
  if (res.error || !res.data) return <Failed what="질문 처리 기록을" detail={res.error?.message ?? '응답이 없습니다.'} onRetry={res.reload} />
  return <><PageHead crumb="답변 · 질문 처리 기록" title="질문 처리 기록" note="질문 본문 없이 분류, 처리 결과, 근거 수, 모델과 소요 시간만 표시합니다." />
    <Section title="조회 기간" note={periodLabel(res.data.periodStart, res.data.periodEnd)}>
      <DateRangeFilter path="/answer/questions" query={query} navigate={navigate} defaultStart={res.data.periodStart} defaultEnd={res.data.periodEnd} today={res.data.today} preserve={{ workspace, result }} />
    </Section>
    <Section title="처리 기록" note={`${res.data.questions.length}건`}><form className="filter-row" onSubmit={(event) => { event.preventDefault(); navigate(withQuery('/answer/questions', { workspace: workspaceDraft.trim(), result: resultDraft, start: res.data.periodStart, end: res.data.periodEnd })) }}>
        <div className="field"><label className="field-label" htmlFor="question-workspace">워크스페이스</label><input id="question-workspace" className="input" placeholder="예: tyit" value={workspaceDraft} onChange={(e) => setWorkspaceDraft(e.target.value)} /></div>
        <div className="field"><label className="field-label" htmlFor="question-result">처리 결과</label><select id="question-result" className="input" value={resultDraft} onChange={(e) => setResultDraft(e.target.value)}><option value="">모든 결과</option><option value="answered">답변 완료</option><option value="no_hits">근거 없음</option><option value="error">오류</option></select></div>
        <div className="filter-actions"><button className="btn btn-primary btn-sm" type="submit">조회</button>{(workspace || result) && <button className="btn btn-sm btn-quiet" type="button" onClick={() => navigate(withQuery('/answer/questions', { start: res.data.periodStart, end: res.data.periodEnd }))}>조건 초기화</button>}</div>
      </form>
      <div className="table-wrap"><table className="table"><thead><tr><th>시각</th><th>워크스페이스</th><th>분류</th><th>결과</th><th className="num">근거</th><th>모델</th><th className="num">비용</th><th className="num">소요 시간</th></tr></thead><tbody>{res.data.questions.map((row, index) => <tr key={`${row.logAt}-${index}`} className={row.reason === 'error' ? 'is-error-row' : undefined}><td>{fmt.dayClock(row.logAt)}</td><td>{row.workspace}</td><td className="mono">{row.intent}/{row.source}</td><td>{row.reason === 'error' ? <button className="table-link" onClick={() => navigate(withQuery('/manage/logs', { workspace: row.workspace, at: row.logAt, level: 'error' }))}>오류 로그 보기</button> : RESULT_LABEL[row.reason] ?? row.reason}</td><td className="num">{row.hits || '-'}</td><td className="mono">{row.model || '-'}</td><td className="num">{row.costUsd ? fmt.usd(row.costUsd) : '-'}</td><td className="num">{fmt.ms(row.ms)}</td></tr>)}</tbody></table></div>
    </Section></>
}
