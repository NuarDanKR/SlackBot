import { useEffect, useMemo, useState } from 'react'
import { ApiError, api } from '../api/client'
import { useResource } from '../api/hooks'
import { Chip, Failed, Loading, Metric, PageHead, Section, fmt } from '../components/primitives'
import type { ConsoleUser, Specialist, SpecialistCall, SpecialistRequest } from '../types'
import { withQuery } from '../navigation'

const RESULT_LABEL: Record<SpecialistCall['result'], string> = { success: '성공', fallback: '마스터 폴백', error: '오류', contract_violation: '계약 위반' }
function stateChip(state: Specialist['state'], health: Specialist['health']) {
  if (state === 'error' || health === 'error') return <Chip tone="bad">장애</Chip>
  if (state === 'enabled') return <Chip tone="ok">사용 중</Chip>
  if (state === 'draft') return <Chip tone="watch">초안</Chip>
  return <Chip tone="plain">사용 중지</Chip>
}

export function SpecialistAnalytics({ query, navigate }: { query: URLSearchParams; navigate: (path: string) => void }) {
  const specialist = query.get('specialist') ?? ''
  const result = query.get('result') ?? ''
  const params = new URLSearchParams({ ...(specialist && { specialist }), ...(result && { result }) })
  const res = useResource<{ calls: SpecialistCall[] }>(`/api/specialist-calls?${params}`)
  if (res.loading) return <Loading what="전문 봇 분석을" />
  if (res.error || !res.data) return <Failed what="전문 봇 분석을" detail={res.error?.message ?? '응답이 없습니다.'} onRetry={res.reload} />
  const calls = res.data.calls
  const success = calls.filter((c) => c.result === 'success').length
  const fallback = calls.filter((c) => c.result === 'fallback').length
  const avg = calls.length ? calls.reduce((sum, c) => sum + c.elapsedMs, 0) / calls.length : 0
  return <><PageHead crumb="답변 · 전문 봇 분석" title="전문 봇 분석" note="마스터 봇의 라우팅 결정과 전문 봇 호출 결과를 업무 본문 없이 분석합니다." />
    <Section title="호출 현황"><div className="metrics overview-metrics"><Metric k="호출" v={fmt.int(calls.length)} unit="건" /><Metric k="성공" v={fmt.int(success)} unit="건" /><Metric k="폴백" v={fmt.int(fallback)} unit="건" /><Metric k="평균 응답" v={fmt.ms(avg)} /></div></Section>
    <Section title="최근 호출"><div className="filter-row"><input className="input" placeholder="전문 봇 키" value={specialist} onChange={(e) => navigate(withQuery('/answer/specialists', { specialist: e.target.value, result }))} /><select className="input" value={result} onChange={(e) => navigate(withQuery('/answer/specialists', { specialist, result: e.target.value }))}><option value="">모든 결과</option><option value="success">성공</option><option value="fallback">마스터 폴백</option><option value="error">오류</option><option value="contract_violation">계약 위반</option></select></div>
      <div className="table-wrap"><table className="table"><thead><tr><th>시각</th><th>워크스페이스</th><th>전문 봇</th><th>선택 이유</th><th>신뢰도</th><th>결과</th><th className="num">시간</th><th className="num">비용</th></tr></thead><tbody>{calls.map((call) => <tr key={call.id}><td>{fmt.dayClock(call.at)}</td><td>{call.workspace}</td><td className="mono">{call.specialist}</td><td>{call.routingReason || '-'}</td><td>{call.confidence == null ? '-' : `${Math.round(call.confidence * 100)}%`}</td><td>{RESULT_LABEL[call.result]}{call.errorCode && <div className="hint mono">{call.errorCode}</div>}</td><td className="num">{fmt.ms(call.elapsedMs)}</td><td className="num">{fmt.usd(call.costUsd)}</td></tr>)}</tbody></table></div>
    </Section></>
}

interface SpecialistResponse { specialists: Specialist[]; requests: SpecialistRequest[]; adapters: { key: string; name: string; domain: string; available: boolean }[] }
export function SpecialistManagement({ user, query, onToast }: { user: ConsoleUser; query: URLSearchParams; onToast: (message: string) => void }) {
  const resource = useResource<SpecialistResponse>('/api/specialists')
  const [data, setData] = useState<SpecialistResponse | null>(null)
  const [draft, setDraft] = useState({ key: '', name: '', domain: '', adapter: 'hermes', state: 'draft', version: '', contractVersion: 'v1', workspaces: '' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => { if (resource.data) setData(resource.data) }, [resource.data])
  const filtered = useMemo(() => {
    const state = query.get('state')
    const specialist = query.get('specialist')
    return (data?.specialists ?? []).filter((row) => (!state || row.state === state || row.health === state) && (!specialist || row.key === specialist))
  }, [data, query])
  if (resource.loading && !data) return <Loading what="전문 봇 레지스트리를" />
  if (resource.error && !data) return <Failed what="전문 봇 레지스트리를" detail={resource.error.message} onRetry={resource.reload} />
  async function requestChange() {
    setBusy(true); setError(null)
    try {
      const result = await api.securePost<{ requests: SpecialistRequest[] }>('/api/specialists/requests', { ...draft, workspaces: draft.workspaces.split(',').map((v) => v.trim()).filter(Boolean) })
      setData((current) => current ? { ...current, requests: result.requests } : current)
      onToast('전문 봇 변경 요청을 등록했습니다.')
    } catch (caught) { setError(caught instanceof ApiError ? caught.message : String(caught)) }
    finally { setBusy(false) }
  }
  async function decide(id: string, decision: 'approve' | 'reject') {
    setBusy(true); setError(null)
    try { const result = await api.securePost<SpecialistResponse>(`/api/specialists/requests/${id}/${decision}`, { note: `${user.email} 콘솔 처리` }); setData(result); onToast(decision === 'approve' ? '승인했습니다.' : '반려했습니다.') }
    catch (caught) { setError(caught instanceof ApiError ? caught.message : String(caught)) }
    finally { setBusy(false) }
  }
  function edit(row: Specialist) {
    setDraft({
      key: row.key,
      name: row.name,
      domain: row.domain,
      adapter: row.adapter,
      state: row.state === 'error' ? 'disabled' : row.state,
      version: row.version,
      contractVersion: row.contractVersion,
      workspaces: row.workspaces.join(', '),
    })
  }
  return <><PageHead crumb="관리 · 전문 봇" title="전문 봇 관리" note="코드에 등록된 어댑터의 상태와 승인된 버전만 관리합니다. 프롬프트와 실행 경로는 배포 절차에서 검토합니다." />
    {error && <div className="notice bad"><div><div className="notice-title">처리하지 못했습니다.</div><div className="notice-detail">{error}</div></div></div>}
    <Section title="등록된 전문 봇" note={`${filtered.length}개`}><div className="table-wrap"><table className="table"><thead><tr><th>전문 봇</th><th>분야</th><th>어댑터</th><th>버전</th><th>적용 범위</th><th>상태</th><th>관리</th></tr></thead><tbody>{filtered.map((row) => <tr key={row.key}><td>{row.name}<div className="ws-key">{row.key}</div></td><td>{row.domain}</td><td className="mono">{row.adapter}{!row.adapterAvailable && <div className="hint">런타임 미배포</div>}</td><td>{row.version || '-'}<div className="hint">계약 {row.contractVersion}</div></td><td>{row.workspaces.join(', ') || '미지정'}</td><td>{stateChip(row.state, row.health)}{row.errorCode && <div className="hint mono">{row.errorCode}</div>}</td><td><button className="btn btn-sm" type="button" onClick={() => edit(row)}>변경 요청</button></td></tr>)}</tbody></table></div></Section>
    <Section title="변경 요청" note={`${data?.requests.length ?? 0}건`}><div className="table-wrap"><table className="table"><thead><tr><th>요청</th><th>전문 봇</th><th>요청자</th><th>상태</th><th>처리</th></tr></thead><tbody>{(data?.requests ?? []).map((row) => <tr key={row.id}><td>#{row.id}<div className="hint">{fmt.dayClock(row.requestedAt)}</div></td><td className="mono">{row.specialist}</td><td>{row.requester}</td><td>{row.state}</td><td>{user.role === 'admin' && row.state === 'awaiting_approval' ? <div className="form-row"><button className="btn btn-sm btn-primary" disabled={busy || row.requester === user.email} onClick={() => decide(row.id, 'approve')}>승인</button><button className="btn btn-sm" disabled={busy} onClick={() => decide(row.id, 'reject')}>반려</button></div> : '-'}</td></tr>)}</tbody></table></div></Section>
    <Section title="변경 요청 등록" lead="개발자는 요청을 등록하고 다른 관리자가 승인합니다. 활성화는 런타임 어댑터가 배포된 뒤에만 가능합니다."><div className="card card-pad"><div className="form-grid specialist-form"><input className="input" placeholder="키 (예: hermes)" value={draft.key} onChange={(e) => setDraft({ ...draft, key: e.target.value })} /><input className="input" placeholder="표시 이름" value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} /><input className="input" placeholder="담당 분야" value={draft.domain} onChange={(e) => setDraft({ ...draft, domain: e.target.value })} /><select className="input" value={draft.adapter} onChange={(e) => setDraft({ ...draft, adapter: e.target.value })}>{(data?.adapters ?? []).map((a) => <option key={a.key} value={a.key}>{a.name} ({a.available ? '배포됨' : '미배포'})</option>)}</select><select className="input" value={draft.state} onChange={(e) => setDraft({ ...draft, state: e.target.value })}><option value="draft">초안</option><option value="enabled">사용</option><option value="disabled">사용 중지</option></select><input className="input" placeholder="배포 버전" value={draft.version} onChange={(e) => setDraft({ ...draft, version: e.target.value })} /><input className="input" placeholder="계약 버전" value={draft.contractVersion} onChange={(e) => setDraft({ ...draft, contractVersion: e.target.value })} /><input className="input" placeholder="워크스페이스 키, 쉼표 구분" value={draft.workspaces} onChange={(e) => setDraft({ ...draft, workspaces: e.target.value })} /></div><div className="form-row"><button className="btn btn-primary" disabled={busy || !draft.key || !draft.name || !draft.domain} onClick={requestChange}>승인 요청</button></div></div></Section>
  </>
}
