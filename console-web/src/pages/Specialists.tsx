import { useEffect, useMemo, useState } from 'react'
import { ApiError, api } from '../api/client'
import { useResource } from '../api/hooks'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { Chip, Failed, Loading, Metric, MiniBars, PageHead, Section, fmt } from '../components/primitives'
import type { ConsoleUser, GatewayModel, Specialist, SpecialistCall, SpecialistRequest } from '../types'
import { withQuery } from '../navigation'

const RESULT_LABEL: Record<SpecialistCall['result'], string> = { success: '성공', fallback: '마스터 폴백', error: '오류', contract_violation: '계약 위반' }
const REQUEST_STATE_LABEL: Record<SpecialistRequest['state'], string> = { awaiting_approval: '승인 대기', approved: '승인됨', rejected: '반려' }
function stateChip(state: Specialist['state'], health: Specialist['health']) {
  if (state === 'error' || health === 'error') return <Chip tone="bad">장애</Chip>
  if (state === 'enabled') return <Chip tone="ok">사용 중</Chip>
  if (state === 'draft') return <Chip tone="watch">초안</Chip>
  return <Chip tone="plain">사용 중지</Chip>
}

export function SpecialistAnalytics({ query, navigate }: { query: URLSearchParams; navigate: (path: string) => void }) {
  const specialist = query.get('specialist') ?? ''
  const result = query.get('result') ?? ''
  const [specialistDraft, setSpecialistDraft] = useState(specialist)
  const [resultDraft, setResultDraft] = useState(result)
  useEffect(() => { setSpecialistDraft(specialist); setResultDraft(result) }, [specialist, result])
  const params = new URLSearchParams({ ...(specialist && { specialist }), ...(result && { result }) })
  const res = useResource<{ calls: SpecialistCall[] }>(`/api/specialist-calls?${params}`)
  if (res.loading) return <Loading what="전문 봇 분석을" />
  if (res.error || !res.data) return <Failed what="전문 봇 분석을" detail={res.error?.message ?? '응답이 없습니다.'} onRetry={res.reload} />
  const calls = res.data.calls
  const success = calls.filter((c) => c.result === 'success').length
  const fallback = calls.filter((c) => c.result === 'fallback').length
  const errors = calls.filter((c) => c.result === 'error').length
  const violations = calls.filter((c) => c.result === 'contract_violation').length
  const avg = calls.length ? calls.reduce((sum, c) => sum + c.elapsedMs, 0) / calls.length : 0
  return <><PageHead crumb="답변 · 전문 봇 분석" title="전문 봇 분석" note="마스터 봇의 라우팅 결정과 전문 봇 호출 결과를 업무 본문 없이 분석합니다." />
    <Section title="호출 현황"><div className="metrics overview-metrics"><Metric k="호출" v={fmt.int(calls.length)} unit="건" /><Metric k="성공" v={fmt.int(success)} unit="건" /><Metric k="폴백" v={fmt.int(fallback)} unit="건" /><Metric k="평균 응답" v={fmt.ms(avg)} /></div></Section>
    <Section title="호출 결과 분포" lead="성공, 마스터 봇 폴백, 오류와 계약 위반 건수를 비교합니다."><MiniBars label="전문 봇 호출 결과" items={[{ label: '성공', value: success, tone: 'ok' }, { label: '마스터 폴백', value: fallback, tone: 'warn' }, { label: '오류', value: errors, tone: 'bad' }, { label: '계약 위반', value: violations, tone: 'bad' }]} /></Section>
    <Section title="최근 호출"><form className="filter-row" onSubmit={(event) => { event.preventDefault(); navigate(withQuery('/answer/specialists', { specialist: specialistDraft.trim(), result: resultDraft })) }}>
      <div className="field"><label className="field-label" htmlFor="specialist-key">전문 봇</label><input id="specialist-key" className="input" placeholder="예: hermes" value={specialistDraft} onChange={(e) => setSpecialistDraft(e.target.value)} /></div>
      <div className="field"><label className="field-label" htmlFor="specialist-result">호출 결과</label><select id="specialist-result" className="input" value={resultDraft} onChange={(e) => setResultDraft(e.target.value)}><option value="">모든 결과</option><option value="success">성공</option><option value="fallback">마스터 폴백</option><option value="error">오류</option><option value="contract_violation">계약 위반</option></select></div>
      <div className="filter-actions"><button className="btn btn-primary btn-sm" type="submit">조회</button>{(specialist || result) && <button className="btn btn-sm btn-quiet" type="button" onClick={() => navigate('/answer/specialists')}>초기화</button>}</div>
    </form>
      <div className="table-wrap"><table className="table"><thead><tr><th>시각</th><th>워크스페이스</th><th>전문 봇</th><th>선택 이유</th><th>신뢰도</th><th>결과</th><th className="num">시간</th><th className="num">비용</th></tr></thead><tbody>{calls.map((call) => <tr key={call.id}><td>{fmt.dayClock(call.at)}</td><td>{call.workspace}</td><td className="mono">{call.specialist}</td><td>{call.routingReason || '-'}</td><td>{call.confidence == null ? '-' : `${Math.round(call.confidence * 100)}%`}</td><td>{RESULT_LABEL[call.result]}{call.errorCode && <div className="hint mono">{call.errorCode}</div>}</td><td className="num">{fmt.ms(call.elapsedMs)}</td><td className="num">{fmt.usd(call.costUsd)}</td></tr>)}</tbody></table></div>
    </Section></>
}

interface SpecialistResponse { specialists: Specialist[]; requests: SpecialistRequest[]; adapters: { key: string; name: string; domain: string; available: boolean }[] }
interface ModelResponse { models: GatewayModel[] }
interface WorkspaceOptions { workspaces: { key: string; label: string }[] }
export function SpecialistManagement({ user, query, onToast }: { user: ConsoleUser; query: URLSearchParams; onToast: (message: string) => void }) {
  const resource = useResource<SpecialistResponse>('/api/specialists')
  // 모델 목록과 워크스페이스 목록은 **서버가 사실이다.** 화면이 따로 들고 있으면
  // 없는 값을 고를 수 있고, 그건 저장할 때가 아니라 질문할 때 조용히 실패한다.
  const models = useResource<ModelResponse>('/api/models')
  const workspaces = useResource<WorkspaceOptions>('/api/status')
  const [data, setData] = useState<SpecialistResponse | null>(null)
  const [draft, setDraft] = useState({
    key: '', name: '', domain: '', adapter: 'hermes', state: 'draft',
    version: '', contractVersion: 'v1', model: '', routingHint: '',
    minConfidence: 0.6, rules: '',
  })
  // 워크스페이스는 쉼표 문자열이 아니라 **고른 목록**으로 다룬다.
  const [picked, setPicked] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [decisionPending, setDecisionPending] = useState<{ id: string; decision: 'approve' | 'reject'; specialist: string; requester: string } | null>(null)
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
      const result = await api.securePost<{ requests: SpecialistRequest[] }>('/api/specialists/requests', { ...draft, workspaces: picked })
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
      model: row.model,
      routingHint: row.routingHint,
      minConfidence: row.minConfidence,
      rules: '',
    })
    setPicked(row.workspaces)
    // 규칙 본문은 목록에 없다(8000자가 표마다 실리면 화면이 무거워진다).
    // 편집을 누른 뒤 상세에서 받아 채운다.
    void api.get<Specialist>(`/api/specialists/${row.key}`)
      .then((full) => setDraft((d) => ({ ...d, rules: full.rules ?? '' })))
      .catch(() => undefined)
  }
  return <><PageHead crumb="관리 · 전문 봇" title="전문 봇 관리"
    note="전문 봇 등록·변경 요청을 만들고 검토합니다. 관리자가 만든 요청도 다른 관리자의 승인을 받아야 적용됩니다." />
    {error && <div className="notice bad"><div><div className="notice-title">처리하지 못했습니다.</div><div className="notice-detail">{error}</div></div></div>}
    <Section title="등록된 전문 봇" note={`${filtered.length}개`}><div className="table-wrap"><table className="table"><thead><tr><th>전문 봇</th><th>분야</th><th>어댑터</th><th>모델</th><th>버전</th><th>적용 범위</th><th>상태</th><th>요청</th></tr></thead><tbody>{filtered.map((row) => <tr key={row.key}><td>{row.name}<div className="ws-key">{row.key}</div></td><td>{row.domain}</td><td className="mono">{row.adapter}{!row.adapterAvailable && <div className="hint">런타임 미배포</div>}</td><td className="mono">{row.model || '기본'}{row.hasRules && <div className="hint">규칙 v{row.rulesVersion}</div>}</td><td>{row.version || '-'}<div className="hint">계약 {row.contractVersion}</div></td><td>{row.workspaces.join(', ') || '미지정'}</td><td>{stateChip(row.state, row.health)}{row.errorCode && <div className="hint mono">{row.errorCode}</div>}</td><td><button className="btn btn-sm" type="button" onClick={() => edit(row)}>변경 요청</button></td></tr>)}</tbody></table></div></Section>
    <Section title="승인 대기 및 이력" note={`${data?.requests.length ?? 0}건`}><div className="table-wrap"><table className="table"><thead><tr><th>요청</th><th>전문 봇</th><th>요청자</th><th>상태</th><th>처리</th></tr></thead><tbody>{(data?.requests ?? []).map((row) => <tr key={row.id}><td>#{row.id}<div className="hint">{fmt.dayClock(row.requestedAt)}</div></td><td className="mono">{row.specialist}</td><td>{row.requester}</td><td>{REQUEST_STATE_LABEL[row.state]}</td><td>{user.role === 'admin' && row.state === 'awaiting_approval' ? <div className="form-row"><button className="btn btn-sm btn-primary" disabled={busy} onClick={() => setDecisionPending({ id: row.id, decision: 'approve', specialist: row.specialist, requester: row.requester })}>승인</button><button className="btn btn-sm" disabled={busy} onClick={() => setDecisionPending({ id: row.id, decision: 'reject', specialist: row.specialist, requester: row.requester })}>반려</button></div> : '-'}</td></tr>)}</tbody></table></div></Section>
    <Section title="등록·변경 요청" lead="개발자와 관리자가 등록 정보와 변경 사항을 제출할 수 있습니다. 다른 관리자가 승인한 뒤에만 적용되며, 활성화는 런타임 어댑터가 배포된 뒤에만 가능합니다."><div className="card card-pad"><div className="form-grid specialist-form">
      <div className="field"><label className="field-label" htmlFor="specialist-draft-key">키</label><input id="specialist-draft-key" className="input" placeholder="예: hermes" value={draft.key} onChange={(e) => setDraft({ ...draft, key: e.target.value })} /></div>
      <div className="field"><label className="field-label" htmlFor="specialist-draft-name">표시 이름</label><input id="specialist-draft-name" className="input" value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} /></div>
      <div className="field"><label className="field-label" htmlFor="specialist-draft-domain">담당 분야</label><input id="specialist-draft-domain" className="input" value={draft.domain} onChange={(e) => setDraft({ ...draft, domain: e.target.value })} /></div>
      <div className="field"><label className="field-label" htmlFor="specialist-draft-adapter">어댑터</label><select id="specialist-draft-adapter" className="input" value={draft.adapter} onChange={(e) => setDraft({ ...draft, adapter: e.target.value })}>{(data?.adapters ?? []).map((a) => <option key={a.key} value={a.key}>{a.name} ({a.available ? '배포됨' : '미배포'})</option>)}</select></div>
      <div className="field"><label className="field-label" htmlFor="specialist-draft-state">상태</label><select id="specialist-draft-state" className="input" value={draft.state} onChange={(e) => setDraft({ ...draft, state: e.target.value })}><option value="draft">초안</option><option value="enabled">사용</option><option value="disabled">사용 중지</option></select></div>
      <div className="field"><label className="field-label" htmlFor="specialist-draft-model">모델</label>
        <select id="specialist-draft-model" className="input" value={draft.model} onChange={(e) => setDraft({ ...draft, model: e.target.value })}>
          <option value="">기본 모델 (게이트웨이 설정)</option>
          {(models.data?.models ?? []).map((m) => (
            <option key={m.model} value={m.model}>
              {m.provider} · {m.model} · ${m.inputPer1M}/{m.outputPer1M}{m.usable ? "" : " (키 미등록)"}
            </option>
          ))}
        </select>
        <div className="field-help">간단한 분야는 싼 모델로 충분합니다. 키가 없는 모델을 고르면 질문할 때 마스터가 답합니다.</div>
      </div>
      <div className="field"><label className="field-label" htmlFor="specialist-draft-hint">라우팅 설명</label>
        <input id="specialist-draft-hint" className="input" maxLength={300} placeholder="예: 회의록·업무 진행 상황·기간별 요약" value={draft.routingHint} onChange={(e) => setDraft({ ...draft, routingHint: e.target.value })} />
        <div className="field-help">라우터가 이 글을 읽고 어느 전문가에게 물을지 정합니다. 질문마다 실리므로 300자까지입니다.</div>
      </div>
      <div className="field"><label className="field-label" htmlFor="specialist-draft-confidence">최소 신뢰도</label>
        <input id="specialist-draft-confidence" className="input" type="number" min={0} max={1} step={0.05} value={draft.minConfidence} onChange={(e) => setDraft({ ...draft, minConfidence: Number(e.target.value) })} />
        <div className="field-help">라우터 판정이 이보다 낮으면 마스터가 직접 답합니다. 틀렸을 때 손해가 큰 분야는 높게 둡니다.</div>
      </div>
      <div className="field"><label className="field-label" htmlFor="specialist-draft-version">배포 버전</label><input id="specialist-draft-version" className="input" placeholder="예: 1.4.2" value={draft.version} onChange={(e) => setDraft({ ...draft, version: e.target.value })} />
        <div className="field-help">그 팀이 배포한 전문 봇의 버전입니다. 표시용이고 동작에 쓰이지 않습니다.</div>
      </div>
      <div className="field"><label className="field-label" htmlFor="specialist-draft-contract">계약 버전</label><input id="specialist-draft-contract" className="input" value={draft.contractVersion} onChange={(e) => setDraft({ ...draft, contractVersion: e.target.value })} />
        <div className="field-help">주고받는 형식의 판(현재 v1). 우리 코드가 검사할 수 있는 값만 승인됩니다 — 배포 버전과 달리 <strong>동작을 바꿉니다</strong>.</div>
      </div>
      <div className="field field-wide"><label className="field-label">적용 워크스페이스</label>
        <div className="checkbox-row">
          {(workspaces.data?.workspaces ?? []).map((ws) => (
            <label key={ws.key} className="checkbox">
              <input type="checkbox" checked={picked.includes(ws.key)}
                onChange={(e) => setPicked((cur) => e.target.checked ? [...cur, ws.key] : cur.filter((k) => k !== ws.key))} />
              {ws.label} <span className="ws-key">{ws.key}</span>
            </label>
          ))}
        </div>
        <div className="field-help">고르지 않으면 이 전문가는 어디에서도 호출되지 않습니다.</div>
      </div>
      <div className="field field-wide"><label className="field-label" htmlFor="specialist-draft-rules">답변 규칙</label>
        <textarea id="specialist-draft-rules" className="input" rows={8} maxLength={8000} placeholder="비워 두면 저장소의 기본 규칙을 씁니다." value={draft.rules} onChange={(e) => setDraft({ ...draft, rules: e.target.value })} />
        <div className="field-help">이 전문가가 답할 때의 지시문입니다. 승인 뒤 다음 답변부터 적용되고, 바뀔 때마다 규칙 버전이 오릅니다. 비우면 저장소 기본값으로 돌아갑니다.</div>
      </div>
    </div><div className="form-row"><button className="btn btn-primary" disabled={busy || !draft.key || !draft.name || !draft.domain} onClick={requestChange}>승인 요청</button></div></div></Section>
    <ConfirmDialog open={decisionPending !== null} title={`전문 봇 변경 요청을 ${decisionPending?.decision === 'approve' ? '승인' : '반려'}할까요?`}
      detail={`${decisionPending?.specialist ?? ''} · 요청자 ${decisionPending?.requester ?? ''}. 처리 결과와 승인자는 감사 기록에 남습니다.`}
      confirmLabel={decisionPending?.decision === 'approve' ? '변경 승인' : '요청 반려'} danger={decisionPending?.decision === 'reject'} busy={busy}
      onCancel={() => setDecisionPending(null)} onConfirm={() => { if (decisionPending) void decide(decisionPending.id, decisionPending.decision).finally(() => setDecisionPending(null)) }} />
  </>
}
