import { useEffect, useState } from 'react'
import { useResource } from '../api/hooks'
import { Chip, Failed, Loading, PageHead, Section, fmt } from '../components/primitives'
import type { AuditEvent } from '../types'
import { withQuery } from '../navigation'

const CATEGORIES = ['', 'archive', 'environment', 'timer', 'deployment', 'workspace', 'console-user', 'llm', 'feedback', 'specialist']
const CATEGORY_LABEL: Record<string, string> = { archive: '원문 열람', environment: '환경 설정', timer: '배치', deployment: '배포', workspace: '워크스페이스', 'console-user': '콘솔 사용자', llm: 'LLM', feedback: '피드백', specialist: '전문 봇' }
const OUTCOME_LABEL: Record<string, string> = { succeeded: '완료', success: '완료', failed: '실패', requested: '요청됨', rejected: '반려', approved: '승인됨' }
const ACTION_LABEL: Record<string, string> = { read: '열람', change: '변경', save: '저장', rotate: '키 교체', 'state-change': '사용 상태 변경', handled: '처리 완료', enable: '사용 시작', disable: '사용 중지', run: '즉시 실행', schedule: '주기 변경', 'direct-deploy': '즉시 배포', request: '요청', approve: '승인', reject: '반려', 'change-request': '변경 요청' }

export function AuditEvents({ query, navigate }: { query: URLSearchParams; navigate: (path: string) => void }) {
  const category = query.get('category') ?? ''
  const workspace = query.get('workspace') ?? ''
  const actor = query.get('actor') ?? ''
  const [draft, setDraft] = useState({ category, workspace, actor })
  useEffect(() => setDraft({ category, workspace, actor }), [category, workspace, actor])
  const params = new URLSearchParams({ ...(category && { category }), ...(workspace && { workspace }), ...(actor && { actor }) })
  const res = useResource<{ events: AuditEvent[] }>(`/api/audit-events?${params}`)
  if (res.loading) return <Loading what="감사 기록을" />
  if (res.error || !res.data) return <Failed what="감사 기록을" detail={res.error?.message ?? '응답이 없습니다.'} onRetry={res.reload} />
  const update = (values: Record<string, string>) => navigate(withQuery('/console/audit', { category, workspace, actor, ...values }))
  return <><PageHead crumb="콘솔 관리 · 감사" title="감사 기록" note="원문이나 시크릿을 저장하지 않고 누가 어떤 관리 작업을 했는지 추적합니다." aside={<Chip tone="plain">최근 {res.data.events.length}건</Chip>} />
    <Section title="이벤트"><form className="filter-row" onSubmit={(event) => { event.preventDefault(); update(draft) }}>
        <div className="field"><label className="field-label" htmlFor="audit-category">분류</label><select id="audit-category" className="input" value={draft.category} onChange={(e) => setDraft({ ...draft, category: e.target.value })}>{CATEGORIES.map((value) => <option key={value || 'all'} value={value}>{value ? CATEGORY_LABEL[value] ?? value : '모든 분류'}</option>)}</select></div>
        <div className="field"><label className="field-label" htmlFor="audit-workspace">워크스페이스</label><input id="audit-workspace" className="input" placeholder="예: tyit" value={draft.workspace} onChange={(e) => setDraft({ ...draft, workspace: e.target.value })} /></div>
        <div className="field"><label className="field-label" htmlFor="audit-actor">행위자</label><input id="audit-actor" className="input" type="email" placeholder="이메일" value={draft.actor} onChange={(e) => setDraft({ ...draft, actor: e.target.value })} /></div>
        <div className="filter-actions"><button className="btn btn-primary btn-sm" type="submit">조회</button>{(category || workspace || actor) && <button className="btn btn-sm btn-quiet" type="button" onClick={() => navigate('/console/audit')}>초기화</button>}</div>
      </form>
      <div className="table-wrap"><table className="table"><thead><tr><th>시각</th><th>행위자</th><th>분류</th><th>동작</th><th>대상</th><th>워크스페이스</th><th>결과</th><th>출처</th></tr></thead><tbody>{res.data.events.map((row) => <tr key={row.id}><td>{fmt.dayClock(String(row.at))}</td><td>{row.actor}</td><td>{CATEGORY_LABEL[row.category] ?? row.category}</td><td>{ACTION_LABEL[row.action] ?? row.action}</td><td><span className="mono">{row.targetType}</span><div className="hint audit-target">{row.targetId}</div></td><td>{row.workspace || '-'}</td><td><Chip tone={row.outcome === 'failed' ? 'bad' : row.outcome === 'requested' ? 'watch' : 'ok'}>{OUTCOME_LABEL[row.outcome] ?? row.outcome}</Chip></td><td>{row.source === 'legacy' ? '기존 로그' : '통합 DB'}</td></tr>)}</tbody></table></div>
    </Section></>
}
