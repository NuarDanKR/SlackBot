import { useResource } from '../api/hooks'
import { Failed, Loading, PageHead, Section, fmt } from '../components/primitives'
import type { CallRow } from '../types'
import { withQuery } from '../navigation'

export function Questions({ query, navigate }: { query: URLSearchParams; navigate: (path: string) => void }) {
  const workspace = query.get('workspace') ?? ''
  const result = query.get('result') ?? ''
  const path = `/api/questions?${new URLSearchParams({ ...(workspace && { workspace }), ...(result && { result }) }).toString()}`
  const res = useResource<{ questions: CallRow[] }>(path)
  if (res.loading) return <Loading what="질문 처리 기록을" />
  if (res.error || !res.data) return <Failed what="질문 처리 기록을" detail={res.error?.message ?? '응답이 없습니다.'} onRetry={res.reload} />
  return <><PageHead crumb="답변 · 질문 처리 기록" title="질문 처리 기록" note="질문 본문 없이 분류, 처리 결과, 근거 수, 모델과 소요 시간만 표시합니다." />
    <Section title="최근 처리" note={`${res.data.questions.length}건`}><div className="filter-row"><input className="input" placeholder="워크스페이스 키" value={workspace} onChange={(e) => navigate(withQuery('/answer/questions', { workspace: e.target.value, result }))} /><select className="input" value={result} onChange={(e) => navigate(withQuery('/answer/questions', { workspace, result: e.target.value }))}><option value="">모든 결과</option><option value="answered">답변 완료</option><option value="no_hits">근거 없음</option><option value="error">오류</option></select></div>
      <div className="table-wrap"><table className="table"><thead><tr><th>시각</th><th>워크스페이스</th><th>분류</th><th>결과</th><th className="num">근거</th><th>모델</th><th className="num">비용</th><th className="num">소요 시간</th></tr></thead><tbody>{res.data.questions.map((row, index) => <tr key={`${row.logAt}-${index}`} className={row.reason === 'error' ? 'is-error-row' : undefined}><td>{fmt.dayClock(row.logAt)}</td><td>{row.workspace}</td><td className="mono">{row.intent}/{row.source}</td><td>{row.reason === 'error' ? <button className="table-link" onClick={() => navigate(withQuery('/manage/logs', { workspace: row.workspace, at: row.logAt, level: 'error' }))}>ERROR 로그 보기</button> : row.reason}</td><td className="num">{row.hits || '-'}</td><td className="mono">{row.model || '-'}</td><td className="num">{row.costUsd ? fmt.usd(row.costUsd) : '-'}</td><td className="num">{fmt.ms(row.ms)}</td></tr>)}</tbody></table></div>
    </Section></>
}
