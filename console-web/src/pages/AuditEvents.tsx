import { useResource } from '../api/hooks'
import { Chip, Failed, Loading, PageHead, Section, fmt } from '../components/primitives'
import type { AuditEvent } from '../types'
import { withQuery } from '../navigation'

const CATEGORIES = ['', 'archive', 'environment', 'timer', 'deployment', 'workspace', 'console-user', 'llm', 'feedback', 'specialist']

export function AuditEvents({ query, navigate }: { query: URLSearchParams; navigate: (path: string) => void }) {
  const category = query.get('category') ?? ''
  const workspace = query.get('workspace') ?? ''
  const actor = query.get('actor') ?? ''
  const params = new URLSearchParams({ ...(category && { category }), ...(workspace && { workspace }), ...(actor && { actor }) })
  const res = useResource<{ events: AuditEvent[] }>(`/api/audit-events?${params}`)
  if (res.loading) return <Loading what="감사 기록을" />
  if (res.error || !res.data) return <Failed what="감사 기록을" detail={res.error?.message ?? '응답이 없습니다.'} onRetry={res.reload} />
  const update = (values: Record<string, string>) => navigate(withQuery('/console/audit', { category, workspace, actor, ...values }))
  return <><PageHead crumb="콘솔 관리 · 감사" title="감사 기록" note="원문이나 시크릿을 저장하지 않고 누가 어떤 관리 작업을 했는지 추적합니다." aside={<Chip tone="plain">최근 {res.data.events.length}건</Chip>} />
    <Section title="이벤트"><div className="filter-row"><select className="input" value={category} onChange={(e) => update({ category: e.target.value })}>{CATEGORIES.map((value) => <option key={value || 'all'} value={value}>{value || '모든 분류'}</option>)}</select><input className="input" placeholder="워크스페이스" value={workspace} onChange={(e) => update({ workspace: e.target.value })} /><input className="input" placeholder="행위자 이메일" value={actor} onChange={(e) => update({ actor: e.target.value })} /></div>
      <div className="table-wrap"><table className="table"><thead><tr><th>시각</th><th>행위자</th><th>분류</th><th>동작</th><th>대상</th><th>워크스페이스</th><th>결과</th><th>출처</th></tr></thead><tbody>{res.data.events.map((row) => <tr key={row.id}><td>{fmt.dayClock(String(row.at))}</td><td>{row.actor}</td><td>{row.category}</td><td>{row.action}</td><td><span className="mono">{row.targetType}</span><div className="hint audit-target">{row.targetId}</div></td><td>{row.workspace || '-'}</td><td><Chip tone={row.outcome === 'failed' ? 'bad' : row.outcome === 'requested' ? 'watch' : 'ok'}>{row.outcome}</Chip></td><td>{row.source === 'legacy' ? '기존 로그' : '통합 DB'}</td></tr>)}</tbody></table></div>
    </Section></>
}
