/** B-12 임베딩 색인 — 모델 등록, 워크스페이스 승인, 색인 현황, 실행 이력.
 *
 * 서버: `src/tybot/console/embedding_routes.py`.
 *
 * ## 저장은 실행이 아니다
 * 이 화면은 정책 행을 고쳐 쓸 뿐이다. 실제 색인은 배치 관리의 「임베딩 색인」
 * 타이머가 30분마다 한 회차씩 돌고, 회차마다 서버가 정책을 다시 판정한다. 그래서
 * 저장 성공을 「색인됨」 으로 보여 주지 않는다.
 *
 * ## 켜기 전에 분량을 먼저 본다
 * 워크스페이스를 켜는 저장 단추는 「미리 보기」 를 지나야 열린다. 미리 보기는
 * 차단 사유와 보낼 분량(문서·글자·추정 비용)을 보여 준다. 미리 본 뒤 칸을 바꾸면
 * 다시 미리 봐야 한다 — 다른 설정을 보고 승인한 것이 되지 않게.
 */
import { useEffect, useMemo, useState } from 'react'
import { ApiError, api } from '../api/client'
import { useResource } from '../api/hooks'
import { Chip, Empty, Failed, Loading, PageHead, Section, fmt } from '../components/primitives'

interface EmbeddingModel {
  revision: string
  provider: string
  model: string
  dimensions: number
  normalized: boolean
  state: 'draft' | 'active' | 'retired'
  externalApproved: boolean
  priceUsdPerMtok: number | null
  createdAt: string | null
  createdBy: string
}

interface WorkspacePolicy {
  workspace: string
  enabled: boolean
  modelRevision: string | null
  transport: 'local' | 'external'
  allowChannel: boolean
  allowAttachment: boolean
  externalTransferApproved: boolean
  revision: number
  reason: string
  updatedAt: string | null
  updatedBy: string
}

interface Coverage {
  workspace: string
  modelRevision: string
  currentDocuments: number
  chunks: number
  failedDocuments: number
  lastIndexedAt: string | null
}

interface IndexRun {
  id: number
  modelRevision: string
  trigger: string
  requestedBy: string
  state: 'running' | 'succeeded' | 'failed'
  errorCode: string
  stoppedReason: string
  requested: number | null
  published: number | null
  embedCalls: number | null
  embeddedChars: number | null
  embeddedChunks: number | null
  costUsd: number | null
  startedAt: string | null
  finishedAt: string | null
}

interface Status {
  providers: string[]
  models: EmbeddingModel[]
  policies: WorkspacePolicy[]
  coverage: Coverage[]
  runs: IndexRun[]
}

interface Preview {
  workspace: string
  currentRevision: number | null
  savable: boolean
  blockers: string[]
  estimate: {
    documents: number
    chars: number
    priceUsdPerMtok: number | null
    estimatedCostUsd: number | null
  } | null
}

interface WorkspaceRow { key: string; label: string }

/** 서버의 고정 코드를 사람 말로. 모르는 코드는 그대로 보인다 — 숨기면 조사할 실마리가 사라진다. */
const REASON: Record<string, string> = {
  activation_blocked: '켤 수 없는 설정입니다',
  model_unknown: '등록되지 않은 모델입니다',
  model_required: '모델을 골라야 켤 수 있습니다',
  model_not_active: '모델이 활성 상태가 아닙니다',
  model_external_not_approved: '모델의 외부 전송 승인이 없습니다',
  workspace_external_not_approved: '이 워크스페이스의 외부 전송 승인이 없습니다',
  provider_requires_external_transport: '외부 서비스 모델은 전송 방식을 「외부」로 적어야 합니다',
  nothing_allowed: '채널 원문과 첨부 중 하나는 허용해야 합니다',
  external_approval_requires_external_transport: '외부 전송 승인은 전송 방식이 「외부」일 때만 줄 수 있습니다',
  policy_conflict: '다른 사람이 먼저 저장했습니다. 새로고침 후 다시 확인해 주세요',
  model_exists: '같은 이름의 모델 revision 이 이미 있습니다',
  model_retired: '퇴역한 모델은 되돌리지 않습니다. 새 revision 으로 등록해 주세요',
  model_in_use: '켜진 워크스페이스가 쓰는 모델입니다. 워크스페이스를 먼저 끄세요',
  provider_unsupported: '임베딩 어댑터가 없는 프로바이더입니다',
  dimensions_invalid: '차원은 1~16000 사이 정수여야 합니다',
  revision_invalid: 'revision 은 영문·숫자·.:_- 만 쓸 수 있습니다',
  model_invalid: '모델 이름 형식이 올바르지 않습니다',
  price_invalid: '단가는 0 이상의 숫자여야 합니다',
  reason_required: '사유를 적어 주세요',
  workspace_forbidden: '이 워크스페이스를 변경할 권한이 없습니다',
  database_unavailable: 'DB 에 연결하지 못했습니다',
  policy_unavailable: '정책을 읽거나 쓰지 못했습니다',
  registry_unavailable: '모델 레지스트리를 읽거나 쓰지 못했습니다',
}

function explain(code: string): string {
  if (code.startsWith('workspace_') && !REASON[code]) return `사용 중인 워크스페이스: ${code.slice(10)}`
  return REASON[code] ?? code
}

function errorText(caught: unknown): string {
  if (caught instanceof ApiError && caught.refusal) {
    const head = explain(caught.refusal.code)
    const tail = caught.refusal.blockers.map(explain)
    return tail.length ? `${head} — ${tail.join(' · ')}` : head
  }
  return caught instanceof ApiError ? caught.message : String(caught)
}

const STATE_LABEL: Record<EmbeddingModel['state'], string> = { draft: '초안', active: '활성', retired: '퇴역' }

function modelChip(model: EmbeddingModel) {
  if (model.state === 'retired') return <Chip tone="plain">퇴역</Chip>
  if (model.state === 'draft') return <Chip tone="watch">초안</Chip>
  return <Chip tone="ok">활성</Chip>
}

function runChip(run: IndexRun) {
  if (run.state === 'running') return <Chip tone="info">실행 중</Chip>
  if (run.state === 'failed') return <Chip tone="bad">실패 · {run.errorCode}</Chip>
  return <Chip tone="ok">완료</Chip>
}

const num = (value: number | null | undefined) => (value == null ? '—' : fmt.int(value))
const when = (iso: string | null) => (iso ? fmt.dayClock(iso) : '—')

// --- 모델 등록 ------------------------------------------------------------------

function ModelRegistry({ status, onSaved, onToast }: { status: Status; onSaved: () => void; onToast: (m: string) => void }) {
  const [draft, setDraft] = useState({ revision: '', provider: status.providers[0] ?? 'openai', model: '', dimensions: '1536', price: '', reason: '' })
  const [reasons, setReasons] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function register() {
    setBusy('register'); setError(null)
    try {
      await api.securePost('/api/embedding/models', {
        revision: draft.revision.trim(),
        provider: draft.provider,
        model: draft.model.trim(),
        dimensions: Number(draft.dimensions),
        priceUsdPerMtok: draft.price.trim() === '' ? null : Number(draft.price),
        reason: draft.reason.trim(),
      })
      onToast(`모델 ${draft.revision.trim()} 을 초안으로 등록했습니다. 활성화와 외부 전송 승인은 따로 합니다.`)
      setDraft({ ...draft, revision: '', model: '', reason: '' })
      onSaved()
    } catch (caught) { setError(errorText(caught)) } finally { setBusy(null) }
  }

  async function change(model: EmbeddingModel, state: EmbeddingModel['state'], externalApproved: boolean, what: string) {
    const reason = (reasons[model.revision] ?? '').trim()
    if (!reason) { setError(`${model.revision}: ${explain('reason_required')}`); return }
    setBusy(model.revision); setError(null)
    try {
      await api.put(`/api/embedding/models/${encodeURIComponent(model.revision)}`, { state, externalApproved, reason })
      onToast(`${model.revision}: ${what}`)
      setReasons({ ...reasons, [model.revision]: '' })
      onSaved()
    } catch (caught) { setError(errorText(caught)) } finally { setBusy(null) }
  }

  const canRegister = draft.revision.trim() && draft.model.trim() && draft.reason.trim() && Number(draft.dimensions) > 0
  return (
    <Section title="1. 임베딩 모델" lead="모델은 초안으로 등록되고, 활성화와 외부 전송 승인은 각각 사유를 적고 따로 합니다. 차원이나 모델을 바꾸려면 새 revision 으로 등록합니다 — 같은 revision 아래에서 바꾸면 옛 벡터와 새 벡터가 섞입니다.">
      {error && <div className="notice bad"><div><div className="notice-title">반영하지 못했습니다</div><div className="notice-detail">{error}</div></div></div>}
      {status.models.length === 0
        ? <Empty title="등록된 모델이 없습니다" note="아래에서 첫 모델을 등록하세요. 등록 전에 서버에서 프로바이더 스모크(scripts/check_embedding_provider.py)로 응답 모양을 확인해 두면 첫 색인에서 비용이 새지 않습니다." />
        : <div className="card card-pad"><div className="table-scroll"><table className="table">
          <thead><tr><th>revision</th><th>프로바이더 · 모델</th><th>차원</th><th>단가 ($/1M)</th><th>상태</th><th>외부 전송</th><th>사유</th><th>변경</th></tr></thead>
          <tbody>{status.models.map((model) => {
            const locked = model.state === 'retired' || busy === model.revision
            return <tr key={model.revision}>
              <td className="mono">{model.revision}</td>
              <td>{model.provider} · <span className="mono">{model.model}</span></td>
              <td className="mono">{model.dimensions}</td>
              <td className="mono">{model.priceUsdPerMtok == null ? '미입력' : model.priceUsdPerMtok}</td>
              <td>{modelChip(model)}</td>
              <td>{model.externalApproved ? <Chip tone="brand">승인됨</Chip> : <Chip tone="plain">미승인</Chip>}</td>
              <td><input className="input" aria-label={`${model.revision} 변경 사유`} placeholder="변경 사유" value={reasons[model.revision] ?? ''} disabled={model.state === 'retired'} onChange={(e) => setReasons({ ...reasons, [model.revision]: e.target.value })} /></td>
              <td><div className="form-row">
                {model.state === 'draft' && <button className="btn btn-sm btn-primary" type="button" disabled={locked} onClick={() => void change(model, 'active', model.externalApproved, '활성화했습니다')}>활성화</button>}
                {model.state !== 'retired' && (model.externalApproved
                  ? <button className="btn btn-sm" type="button" disabled={locked} onClick={() => void change(model, model.state, false, '외부 전송 승인을 철회했습니다')}>외부 승인 철회</button>
                  : <button className="btn btn-sm" type="button" disabled={locked} onClick={() => void change(model, model.state, true, '외부 전송을 승인했습니다')}>외부 전송 승인</button>)}
                {model.state !== 'retired' && <button className="btn btn-sm btn-danger" type="button" disabled={locked} onClick={() => void change(model, 'retired', model.externalApproved, '퇴역시켰습니다')}>퇴역</button>}
              </div></td>
            </tr>
          })}</tbody>
        </table></div></div>}

      <div className="card card-pad" style={{ marginTop: 16 }}>
        <div className="card-title">새 모델 등록</div>
        <div className="form-grid" style={{ marginTop: 12 }}>
          <label className="field"><span className="field-label">revision</span><input className="input mono" placeholder="oai-3small-1536-v1" value={draft.revision} onChange={(e) => setDraft({ ...draft, revision: e.target.value })} /><span className="field-help">색인 행이 이 이름으로 묶입니다. 바꿀 수 없습니다.</span></label>
          <label className="field"><span className="field-label">프로바이더</span><select className="input" value={draft.provider} onChange={(e) => setDraft({ ...draft, provider: e.target.value })}>{status.providers.map((p) => <option key={p} value={p}>{p}</option>)}</select></label>
          <label className="field"><span className="field-label">모델</span><input className="input mono" placeholder="text-embedding-3-small" value={draft.model} onChange={(e) => setDraft({ ...draft, model: e.target.value })} /></label>
          <label className="field"><span className="field-label">차원</span><input className="input mono" type="number" min="1" max="16000" value={draft.dimensions} onChange={(e) => setDraft({ ...draft, dimensions: e.target.value })} /></label>
          <label className="field"><span className="field-label">입력 단가 ($ / 100만 토큰)</span><input className="input mono" type="number" min="0" step="0.001" placeholder="비워 두면 비용을 「모름」 으로 기록" value={draft.price} onChange={(e) => setDraft({ ...draft, price: e.target.value })} /><span className="field-help">없으면 비용 추정이 표시되지 않습니다(0 으로 간주하지 않습니다).</span></label>
          <label className="field field-wide"><span className="field-label">사유</span><input className="input" value={draft.reason} onChange={(e) => setDraft({ ...draft, reason: e.target.value })} /></label>
        </div>
        <div className="form-row" style={{ marginTop: 14 }}>
          <button className="btn btn-primary" type="button" disabled={!canRegister || busy !== null} onClick={() => void register()}>초안으로 등록</button>
        </div>
      </div>
    </Section>
  )
}

// --- 워크스페이스 정책 ------------------------------------------------------------

interface PolicyDraft {
  workspace: string
  enabled: boolean
  modelRevision: string
  transport: 'local' | 'external'
  allowChannel: boolean
  allowAttachment: boolean
  externalTransferApproved: boolean
  reason: string
}

function draftFor(workspace: string, policy: WorkspacePolicy | undefined, models: EmbeddingModel[]): PolicyDraft {
  const active = models.find((m) => m.state === 'active')
  return {
    workspace,
    enabled: policy?.enabled ?? false,
    modelRevision: policy?.modelRevision ?? active?.revision ?? '',
    transport: policy?.transport ?? 'external',
    allowChannel: policy?.allowChannel ?? true,
    allowAttachment: policy?.allowAttachment ?? true,
    externalTransferApproved: policy?.externalTransferApproved ?? false,
    reason: '',
  }
}

function WorkspacePolicies({ status, onSaved, onToast }: { status: Status; onSaved: () => void; onToast: (m: string) => void }) {
  const workspaces = useResource<{ workspaces: WorkspaceRow[] }>('/api/workspaces')
  const keys = useMemo(() => {
    const listed = (workspaces.data?.workspaces ?? []).map((w) => w.key)
    return Array.from(new Set([...listed, ...status.policies.map((p) => p.workspace)])).sort()
  }, [workspaces.data, status.policies])
  const labels = Object.fromEntries((workspaces.data?.workspaces ?? []).map((w) => [w.key, w.label]))
  const [selected, setSelected] = useState('')
  const policy = status.policies.find((p) => p.workspace === selected)
  const [draft, setDraft] = useState<PolicyDraft | null>(null)
  const [preview, setPreview] = useState<{ draft: string; result: Preview } | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => { if (!selected && keys.length) setSelected(keys[0]) }, [keys, selected])
  useEffect(() => {
    if (!selected) return
    setDraft(draftFor(selected, policy, status.models)); setPreview(null); setError(null)
  }, [selected, policy, status.models])

  if (!draft) return null
  const body = { ...draft, modelRevision: draft.modelRevision || null, expectedRevision: policy?.revision ?? null, reason: draft.reason.trim() }
  const fingerprint = JSON.stringify(body)
  const previewed = preview && preview.draft === fingerprint ? preview.result : null
  const usable = status.models.filter((m) => m.state !== 'retired')

  async function runPreview() {
    setBusy(true); setError(null)
    try {
      const result = await api.securePost<Preview>('/api/embedding/policy/preview', body)
      setPreview({ draft: fingerprint, result })
    } catch (caught) { setError(errorText(caught)) } finally { setBusy(false) }
  }

  async function save() {
    setBusy(true); setError(null)
    try {
      await api.put('/api/embedding/policy', body)
      onToast(draft!.enabled
        ? `${selected}: 색인을 허용했습니다. 배치 관리의 「임베딩 색인」 다음 회차부터 반영됩니다.`
        : `${selected}: 색인을 껐습니다. 이미 만든 색인은 남아 있지만 새로 보내지 않습니다.`)
      setPreview(null)
      onSaved()
    } catch (caught) { setError(errorText(caught)) } finally { setBusy(false) }
  }

  const set = (patch: Partial<PolicyDraft>) => setDraft({ ...draft, ...patch })
  const external = draft.transport === 'external'
  return (
    <Section title="2. 워크스페이스 승인" lead="워크스페이스마다 색인을 켜고 끕니다. 켜려면 활성 모델과, 외부 서비스 모델이면 모델·워크스페이스 양쪽의 외부 전송 승인이 있어야 합니다. 끄는 것은 언제나 할 수 있습니다. DM 은 색인하지 않습니다.">
      {workspaces.error && !workspaces.data && <Failed what="워크스페이스 목록을" detail={workspaces.error.message} onRetry={workspaces.reload} />}
      {error && <div className="notice bad"><div><div className="notice-title">반영하지 못했습니다</div><div className="notice-detail">{error}</div></div></div>}
      <div className="card card-pad">
        <div className="form-grid">
          <label className="field"><span className="field-label">워크스페이스</span>
            <select className="input" value={selected} onChange={(e) => setSelected(e.target.value)}>
              {keys.map((k) => <option key={k} value={k}>{labels[k] ? `${labels[k]} (${k})` : k}</option>)}
            </select>
            <span className="field-help">{policy ? `정책 r${policy.revision} · ${policy.updatedBy} · ${when(policy.updatedAt)}` : '아직 정책이 없습니다(꺼진 상태).'}</span>
          </label>
          <label className="field"><span className="field-label">모델</span>
            <select className="input mono" value={draft.modelRevision} onChange={(e) => set({ modelRevision: e.target.value })}>
              <option value="">(없음)</option>
              {usable.map((m) => <option key={m.revision} value={m.revision}>{m.revision} · {STATE_LABEL[m.state]}</option>)}
            </select>
          </label>
          <label className="field"><span className="field-label">전송 방식</span>
            <select className="input" value={draft.transport} onChange={(e) => set({ transport: e.target.value as PolicyDraft['transport'], externalTransferApproved: e.target.value === 'external' ? draft.externalTransferApproved : false })}>
              <option value="external">외부 서비스로 전송 (예: OpenAI)</option>
              <option value="local">사내 서버에서 처리</option>
            </select>
            <span className="field-help">외부 서비스 모델을 「사내」로 적으면 저장되지 않습니다.</span>
          </label>
        </div>
        <div className="form-row" style={{ marginTop: 12, flexWrap: 'wrap' }}>
          <label className="check-line"><input type="checkbox" checked={draft.allowChannel} onChange={(e) => set({ allowChannel: e.target.checked })} /> 채널 원문</label>
          <label className="check-line"><input type="checkbox" checked={draft.allowAttachment} onChange={(e) => set({ allowAttachment: e.target.checked })} /> 첨부 변환본</label>
          <label className="check-line"><input type="checkbox" checked={draft.externalTransferApproved} disabled={!external} onChange={(e) => set({ externalTransferApproved: e.target.checked })} /> 이 워크스페이스 원문의 외부 전송을 승인합니다</label>
          <label className="check-line"><input type="checkbox" checked={draft.enabled} onChange={(e) => set({ enabled: e.target.checked })} /> <strong>색인 켜기</strong></label>
        </div>
        <label className="field" style={{ marginTop: 12 }}><span className="field-label">사유</span><input className="input" placeholder="예: 키워드 검색 무근거 질문 감소 (B-12)" value={draft.reason} onChange={(e) => set({ reason: e.target.value })} /></label>

        {previewed && <div className={`notice ${previewed.savable ? 'info' : 'bad'}`} style={{ marginTop: 14 }}><div>
          <div className="notice-title">{previewed.savable ? '저장할 수 있습니다' : '이대로는 저장할 수 없습니다'}</div>
          {previewed.blockers.length > 0 && <ul className="notice-detail">{previewed.blockers.map((b) => <li key={b}>{explain(b)}</li>)}</ul>}
          {previewed.estimate && <div className="notice-detail">
            처음 보낼 분량(원문 캐시 기준 추정): 문서 {fmt.int(previewed.estimate.documents)}개 · {fmt.int(previewed.estimate.chars)}자
            {' · '}추정 비용 {previewed.estimate.estimatedCostUsd == null ? '모름(모델 단가 미입력)' : fmt.usd(previewed.estimate.estimatedCostUsd)}.
            {' '}한 회차는 문서 100개 · 500만 자로 묶여 30분마다 나눠 보냅니다.
          </div>}
        </div></div>}

        <div className="form-row" style={{ marginTop: 14 }}>
          <button className="btn" type="button" disabled={busy || !draft.reason.trim()} onClick={() => void runPreview()}>미리 보기</button>
          <button className={`btn ${draft.enabled ? 'btn-primary' : 'btn-danger'}`} type="button" disabled={busy || !previewed?.savable} onClick={() => void save()}>
            {draft.enabled ? '승인 저장' : '끈 상태로 저장'}
          </button>
          {!previewed && <span className="hint">저장하려면 지금 설정으로 미리 보기를 먼저 확인합니다.</span>}
        </div>
      </div>
    </Section>
  )
}

// --- 화면 ---------------------------------------------------------------------

export function EmbeddingIndex({ onToast }: { onToast: (message: string) => void }) {
  const resource = useResource<Status>('/api/embedding/status')
  if (resource.loading && !resource.data) return <Loading what="임베딩 색인 상태를" />
  if (resource.error && !resource.data) return <Failed what="임베딩 색인 상태를" detail={errorText(resource.error)} onRetry={resource.reload} />
  const status = resource.data!
  const enabled = status.policies.filter((p) => p.enabled).length
  const documents = status.coverage.reduce((sum, c) => sum + c.currentDocuments, 0)
  const running = status.runs.find((r) => r.state === 'running')
  return (
    <>
      <PageHead
        crumb="운영 관리"
        title="임베딩 색인"
        note="키워드로 못 찾는 질문을 뜻으로 찾기 위한 벡터 색인(B-12)입니다. 색인은 검색 후보만 넓히고, 답변 근거는 언제나 현재 권한으로 다시 연 원문입니다."
        aside={<>
          <Chip tone={enabled ? 'ok' : 'plain'}>켜진 워크스페이스 {enabled}</Chip>
          <Chip tone="plain">색인 문서 {fmt.int(documents)}</Chip>
          {running && <Chip tone="info">실행 중 #{running.id}</Chip>}
          <button className="btn btn-sm" type="button" onClick={resource.reload}>새로고침</button>
        </>}
      />
      <div className="notice info"><div>
        <div className="notice-title">켜는 순서</div>
        <div className="notice-detail">① 모델 등록 → ② 모델 활성화 · 외부 전송 승인 → ③ 워크스페이스 승인(미리 보기로 분량 확인) → ④ <a href="#/manage/batches">배치 관리</a>에서 「임베딩 색인」 사용 시작. 타이머는 켜진 워크스페이스가 없으면 모델을 부르지 않습니다.</div>
      </div></div>

      <ModelRegistry status={status} onSaved={resource.reload} onToast={onToast} />
      <WorkspacePolicies status={status} onSaved={resource.reload} onToast={onToast} />

      <Section title="3. 색인 현황" lead="워크스페이스·모델별로 현재 검색에 쓰이는 문서와 청크 수입니다.">
        {status.coverage.length === 0
          ? <Empty title="아직 색인된 문서가 없습니다" note="워크스페이스를 켜고 타이머가 한 회차 돌면 여기에 나타납니다." />
          : <div className="card card-pad"><div className="table-scroll"><table className="table">
            <thead><tr><th>워크스페이스</th><th>모델</th><th>현재 문서</th><th>청크</th><th>실패 문서</th><th>마지막 색인</th></tr></thead>
            <tbody>{status.coverage.map((c) => <tr key={`${c.workspace}:${c.modelRevision}`}>
              <td>{c.workspace}</td><td className="mono">{c.modelRevision}</td><td className="mono">{fmt.int(c.currentDocuments)}</td>
              <td className="mono">{fmt.int(c.chunks)}</td><td className="mono">{c.failedDocuments ? <Chip tone="bad">{fmt.int(c.failedDocuments)}</Chip> : 0}</td>
              <td className="mono">{when(c.lastIndexedAt)}</td>
            </tr>)}</tbody>
          </table></div></div>}
      </Section>

      <Section title="4. 실행 이력" lead="회차마다 보낸 분량과 비용입니다. 비용이 「모름」이면 프로바이더가 사용량을 알려 주지 않았거나 모델 단가가 비어 있는 것입니다(0 이 아닙니다).">
        {status.runs.length === 0
          ? <Empty title="실행 기록이 없습니다" note="전체 워크스페이스를 볼 수 있는 관리자에게만 표시됩니다." />
          : <div className="card card-pad"><div className="table-scroll"><table className="table">
            <thead><tr><th>#</th><th>시작</th><th>계기</th><th>모델</th><th>상태</th><th>요청 문서</th><th>발행</th><th>호출</th><th>글자</th><th>비용</th><th>멈춘 이유</th></tr></thead>
            <tbody>{status.runs.map((r) => <tr key={r.id}>
              <td className="mono">{r.id}</td><td className="mono">{when(r.startedAt)}</td><td>{r.trigger}</td><td className="mono">{r.modelRevision}</td>
              <td>{runChip(r)}</td><td className="mono">{num(r.requested)}</td><td className="mono">{num(r.published)}</td>
              <td className="mono">{num(r.embedCalls)}</td><td className="mono">{num(r.embeddedChars)}</td>
              <td className="mono">{r.costUsd == null ? '모름' : fmt.usd(r.costUsd)}</td><td className="mono">{r.stoppedReason || '—'}</td>
            </tr>)}</tbody>
          </table></div></div>}
      </Section>
    </>
  )
}
