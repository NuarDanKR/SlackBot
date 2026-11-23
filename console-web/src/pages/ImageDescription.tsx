import { useState } from 'react'
import { ApiError, api } from '../api/client'
import { useResource } from '../api/hooks'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { Chip, Failed, Loading, PageHead, Section, fmt } from '../components/primitives'

/** B-81 이미지 설명·OCR 전사 운영 화면. 자격 API 는 Vision 전용 DB 에 저장합니다. */

interface ProviderSecret {
  provider: string
  mask: string
  enabled: boolean
  updatedBy: string
  updatedAt: string | null
}

interface CatalogRow {
  provider: string
  model: string
  supportsVision: boolean
  zeroCostVerified: boolean
  verifiedAt: string | null
  maxSensitivity: string
  credentialReady: boolean
  quotaState: string
  quotaRemaining: number | null
}

interface JobRow {
  id: string
  state: string
  errorCode: string
  promptRevision: string
  channelId: string
  fileId: string
  actualModel: string | null
  actualProvider: string | null
  createdAt: string
  finishedAt: string | null
}

interface Snapshot {
  workspace: string
  policy: {
    revision: string
    effectiveEnabled: boolean
    globalEnabled: boolean
    workspaceEnabled: boolean
    provider: string
    model: string
    applicationState: string
  }
  catalog: CatalogRow[]
  jobs: JobRow[]
  counts: { activeApprovals: number; activeClearances: number }
}

const JOB_STATE: Record<string, { tone: 'ok' | 'watch' | 'bad' | 'plain'; text: string }> = {
  described: { tone: 'ok', text: '완료' },
  uncertain: { tone: 'watch', text: '불확실' },
  failed: { tone: 'bad', text: '실패' },
  blocked: { tone: 'bad', text: '차단' },
  unknown: { tone: 'bad', text: '결과 미확정' },
  reserved: { tone: 'plain', text: '대기' },
  dispatching: { tone: 'plain', text: '전송 중' },
  cancelled: { tone: 'plain', text: '취소' },
}

function SecretsSection({ onToast }: { onToast: (message: string) => void }) {
  const secrets = useResource<{ secrets: ProviderSecret[] }>('/api/archiver/image-description/provider-secrets')
  const [provider, setProvider] = useState('google')
  const [key, setKey] = useState('')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [confirm, setConfirm] = useState<null | { enabled?: boolean; replace: boolean; provider: string }>(null)

  async function send(target: string, body: { key?: string; enabled?: boolean }) {
    setBusy(true)
    setError(null)
    try {
      await api.put('/api/archiver/image-description/provider-secrets', { provider: target, reason: reason.trim(), ...body })
      // 키 값은 화면 상태에 남기지 않습니다. 보낸 뒤 바로 지웁니다.
      setKey('')
      setReason('')
      secrets.reload()
      onToast('Vision 전용 자격을 저장했습니다. 정책이 꺼져 있으면 모델은 호출되지 않습니다.')
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught))
    } finally {
      setBusy(false)
    }
  }

  if (secrets.loading && !secrets.data) return <Loading what="Vision 자격을" />
  if (secrets.error && !secrets.data) return <Failed what="Vision 자격을" detail={secrets.error.message} onRetry={secrets.reload} />
  const rows = secrets.data?.secrets ?? []

  return (
    <Section
      title="Vision 전용 API 자격"
      lead={
        '이미지 설명 worker 만 쓰는 키입니다. 일반 TYBot 키나 환경변수로 내려가지 않습니다. ' +
        'DB 에는 암호문·가린 값만 저장되고 저장한 값은 다시 볼 수 없습니다. 키가 있어도 정책·무료 카탈로그가 ' +
        '확인되기 전에는 모델을 부르지 않습니다.'
      }
    >
      {error && <div className="notice bad"><div className="notice-kind">저장 실패</div><div>{error}</div></div>}
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>Provider</th><th>현재 키</th><th>상태</th><th>변경</th><th>관리</th></tr></thead>
          <tbody>
            {rows.length === 0 && <tr><td colSpan={5} className="hint">저장된 Vision 자격이 없습니다.</td></tr>}
            {rows.map((row) => (
              <tr key={row.provider}>
                <td className="mono">{row.provider}</td>
                <td className="mono">{row.mask}</td>
                <td>{row.enabled ? <Chip tone="ok">사용</Chip> : <Chip tone="watch">사용 중지</Chip>}</td>
                <td>{row.updatedAt ? fmt.dayClock(row.updatedAt) : '-'} · {row.updatedBy}</td>
                <td className="nowrap">
                  <button className="btn btn-sm" disabled={busy || !reason.trim()}
                    onClick={() => setConfirm({ enabled: !row.enabled, replace: false, provider: row.provider })}>
                    {row.enabled ? '사용 중지' : '사용'}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="card card-pad model-setting">
        <div className="field">
          <label className="field-label" htmlFor="vision-provider">Provider</label>
          <input id="vision-provider" className="input mono" value={provider} onChange={(e) => setProvider(e.target.value.trim())} />
        </div>
        <div className="field">
          <label className="field-label" htmlFor="vision-key">새 키 (교체)</label>
          <input id="vision-key" className="input mono" type="password" autoComplete="new-password"
            placeholder="붙여 넣으면 저장 시 교체됩니다" value={key} onChange={(e) => setKey(e.target.value)} />
        </div>
        <div className="field">
          <label className="field-label" htmlFor="vision-reason">변경 사유 (감사 기록, 필수)</label>
          <input id="vision-reason" className="input" maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
        </div>
        <div className="form-row">
          <button className="btn btn-primary" disabled={busy || key.trim().length < 16 || !reason.trim() || !provider}
            onClick={() => setConfirm({ replace: true, provider })}>키 저장</button>
        </div>
      </div>
      <ConfirmDialog open={confirm !== null}
        title={confirm?.replace ? `${confirm.provider} 키를 교체할까요?` : 'Vision 자격 상태를 바꿀까요?'}
        detail="저장해도 정책·worker 는 켜지지 않습니다. 실행자와 사유는 감사 기록에 남습니다."
        confirmLabel="저장" busy={busy} onCancel={() => setConfirm(null)}
        onConfirm={() => {
          const target = confirm
          if (!target) return
          const body = target.replace ? { key: key.trim() } : { enabled: target.enabled }
          void send(target.provider, body).finally(() => setConfirm(null))
        }} />
    </Section>
  )
}

function StatusSection({ workspace }: { workspace: string }) {
  const snapshot = useResource<Snapshot>(`/api/archiver/image-description/policy?workspace=${encodeURIComponent(workspace)}`)
  if (snapshot.loading && !snapshot.data) return <Loading what="이미지 설명 현황을" />
  if (snapshot.error && !snapshot.data) return <Failed what="이미지 설명 현황을" detail={snapshot.error.message} onRetry={snapshot.reload} />
  const data = snapshot.data
  if (!data) return null
  return (
    <Section
      title="현황"
      lead="설명·전사 본문과 원본은 이 화면에 나오지 않습니다. 정책·무료 모델·작업 상태만 보입니다."
    >
      <div className="form-row">
        {data.policy.effectiveEnabled ? <Chip tone="ok">정책 켜짐</Chip> : <Chip tone="watch">정책 꺼짐(기본)</Chip>}
        <span className="hint mono">정책 판 {data.policy.revision}</span>
        <span className="hint">모델 {data.policy.provider || '-'}/{data.policy.model || '-'}</span>
        <span className="hint" title="2026-11-20 부터 사진별 승인·PII clearance 없이 정책만으로 보냅니다. 옛 기록은 감사용으로 남습니다.">
          옛 건별 승인 {data.counts.activeApprovals} · 옛 clearance {data.counts.activeClearances} (필수 아님)
        </span>
      </div>
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>모델</th><th>Vision</th><th>무료 확인</th><th>자격</th><th>무료 한도</th></tr></thead>
          <tbody>
            {data.catalog.length === 0 && <tr><td colSpan={5} className="hint">카탈로그가 비어 있습니다.</td></tr>}
            {data.catalog.map((row) => (
              <tr key={`${row.provider}/${row.model}`}>
                <td className="mono">{row.provider}/{row.model}</td>
                <td>{row.supportsVision ? '예' : '아니오'}</td>
                <td>{row.zeroCostVerified ? <Chip tone="ok">확인</Chip> : <Chip tone="bad">미확인</Chip>}</td>
                <td>{row.credentialReady ? '준비' : '없음'}</td>
                <td>{row.quotaState}{row.quotaRemaining !== null ? ` · ${row.quotaRemaining}` : ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>상태</th><th>사유</th><th>채널·파일</th><th>프롬프트 판</th><th>실제 모델</th><th>시각</th></tr></thead>
          <tbody>
            {data.jobs.length === 0 && <tr><td colSpan={6} className="hint">작업이 없습니다.</td></tr>}
            {data.jobs.map((job) => {
              const state = JOB_STATE[job.state] ?? { tone: 'plain' as const, text: job.state }
              return (
                <tr key={job.id}>
                  <td><Chip tone={state.tone}>{state.text}</Chip></td>
                  <td className="mono">{job.errorCode || '-'}</td>
                  <td className="mono">{job.channelId}/{job.fileId}</td>
                  <td className="mono">{job.promptRevision}</td>
                  <td className="mono">{job.actualProvider ? `${job.actualProvider}/${job.actualModel}` : '-'}</td>
                  <td>{fmt.dayClock(job.finishedAt ?? job.createdAt)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </Section>
  )
}

/* ---------- B-80 PDF 쪽 전사: 정책·요금표·예산 ---------- */

interface PdfVisionJob {
  id: string
  state: string
  fileId: string
  page: number
  reservedUsd: string | null
  actualUsd: string | null
  createdAt: string
  finishedAt: string | null
}

interface PdfVisionSnapshot {
  workspace: string
  policy: {
    revision: string
    globalEnabled: boolean
    workspaceEnabled: boolean
    workspaceRowExists: boolean
    global: Record<string, unknown>
    workspace: Record<string, unknown>
    effective: Record<string, unknown>
    tariffConfigured: boolean
    blockers: string[]
  }
  todayChargedUsd: string | null
  counts: Record<string, number>
  jobs: PdfVisionJob[]
}

/** 화면 칸 → 원장 설정 키. 숫자 칸은 정수/실수, 금액은 문자열(정밀도)로 보냅니다.
 *  globalOnly: 모델·요금표는 전역 예산의 근거라 전역 행에서만 고칩니다(서버도 막습니다). */
const PDF_FIELDS: { key: string; label: string; kind: 'text' | 'int' | 'float' | 'usd'; hint?: string; globalOnly?: boolean }[] = [
  { key: 'provider', label: 'Provider', kind: 'text', hint: 'google · anthropic · openai', globalOnly: true },
  { key: 'model', label: '모델', kind: 'text', globalOnly: true },
  { key: 'input_usd_per_mtok', label: '입력 요금 (USD / 1M tokens)', kind: 'usd', hint: 'provider 공식 가격표에서 확인한 값 · 0 불가', globalOnly: true },
  { key: 'output_usd_per_mtok', label: '출력 요금 (USD / 1M tokens)', kind: 'usd', hint: '사고(thinking) 토큰 포함 출력 단가 · 0 불가', globalOnly: true },
  { key: 'job_usd', label: '쪽당 예약 상한 (USD)', kind: 'usd', hint: '출력 상한 × 출력 요금 이상이어야 합니다' },
  { key: 'daily_usd', label: '하루 예산 (USD)', kind: 'usd', hint: '전역 행에 반드시 있어야 합니다' },
  { key: 'max_output_tokens', label: '출력 토큰 상한', kind: 'int' },
  { key: 'max_image_bytes', label: '쪽 이미지 최대 바이트', kind: 'int' },
  { key: 'timeout_seconds', label: '시간 제한 (초)', kind: 'float' },
  { key: 'max_concurrency', label: '동시 작업 상한', kind: 'int', hint: '전역 행에 반드시 있어야 합니다' },
]

const PDF_JOB_STATE: Record<string, { tone: 'ok' | 'watch' | 'bad' | 'plain'; text: string }> = {
  settled: { tone: 'ok', text: '정산' },
  unknown: { tone: 'bad', text: '비용 미확정' },
  reserved: { tone: 'plain', text: '예약' },
  dispatching: { tone: 'plain', text: '전송 중' },
  cancelled: { tone: 'plain', text: '취소' },
}

const PDF_BLOCKER: Record<string, string> = {
  job_usd_below_output_bound: '쪽당 예약 상한이 「출력 토큰 상한 × 출력 요금」 보다 작습니다. 이대로면 모든 예약이 거절됩니다.',
}

function blockerText(code: string): string {
  if (PDF_BLOCKER[code]) return PDF_BLOCKER[code]
  const override = code.match(/^workspace_overrides_(.+)$/)
  if (override) return `워크스페이스 행이 ${override[1]} 를 덮어쓰고 있습니다. 모델·요금표는 전역 행만 정합니다.`
  if (['provider', 'model', 'input_usd_per_mtok', 'output_usd_per_mtok'].includes(code)) return `${code} 는 전역 기본값에서만 고칠 수 있습니다.`
  const globalKey = code.match(/^activation_requires_global_(.+)$/)
  if (globalKey) return `전역 행에 ${globalKey[1]} 가 필요합니다.`
  const key = code.match(/^activation_requires_(.+)$/)
  if (key) return `${key[1]} 이(가) 설정되지 않았습니다.`
  return code
}

function PdfVisionSection({ workspace, canEditGlobal, onToast }: { workspace: string; canEditGlobal: boolean; onToast: (message: string) => void }) {
  const snapshot = useResource<PdfVisionSnapshot>(`/api/archiver/pdf-vision?workspace=${encodeURIComponent(workspace)}`)
  const [scope, setScope] = useState<'workspace' | 'global'>('workspace')
  const [draft, setDraft] = useState<Record<string, string>>({})
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<{ message: string; blockers: string[] } | null>(null)
  const [confirm, setConfirm] = useState(false)

  if (snapshot.loading && !snapshot.data) return <Loading what="PDF 쪽 전사 현황을" />
  if (snapshot.error && !snapshot.data) return <Failed what="PDF 쪽 전사 현황을" detail={snapshot.error.message} onRetry={snapshot.reload} />
  const data = snapshot.data
  if (!data) return null

  const row = scope === 'global' ? data.policy.global : data.policy.workspace
  const rowEnabled = scope === 'global' ? data.policy.globalEnabled : data.policy.workspaceEnabled
  const effectiveEnabled = enabled ?? rowEnabled
  const effective = data.policy.effective
  const revision = data.policy.revision

  function config(): Record<string, unknown> {
    const out: Record<string, unknown> = {}
    for (const field of PDF_FIELDS) {
      const raw = (draft[field.key] ?? '').trim()
      if (!raw) continue
      if (field.kind === 'int') out[field.key] = Number.parseInt(raw, 10)
      else if (field.kind === 'float') out[field.key] = Number(raw)
      else out[field.key] = raw
    }
    return out
  }

  async function save() {
    setBusy(true)
    setError(null)
    try {
      await api.put('/api/archiver/pdf-vision/policy', {
        workspace: scope === 'global' ? '' : workspace,
        expectedRevision: revision,
        enabled: effectiveEnabled,
        config: config(),
        reason: reason.trim(),
      })
      setDraft({})
      setEnabled(null)
      setReason('')
      snapshot.reload()
      onToast('PDF 쪽 전사 정책을 저장했습니다. 저장만으로 모델이 호출되거나 소급이 시작되지는 않습니다.')
    } catch (caught) {
      if (caught instanceof ApiError) setError({ message: caught.message, blockers: caught.refusal?.blockers ?? [] })
      else setError({ message: String(caught), blockers: [] })
    } finally {
      setBusy(false)
    }
  }

  function placeholder(key: string): string {
    if (row[key] !== undefined) return `지금 ${String(row[key])}`
    if (effective[key] !== undefined) return `상속 ${String(effective[key])}`
    return '비어 있음'
  }

  return (
    <Section
      title="PDF 쪽 전사 (B-80) — 정책·요금표·예산"
      lead={
        '로컬 추출·OCR 이 못 읽은 PDF 쪽만 Vision 모델로 보냅니다. 비용은 「응답 토큰 × 여기 적은 요금표」 로 정산합니다 — ' +
        'gateway 가 말하는 비용은 쓰지 않습니다. 요금표가 없으면 켤 수 없고, 토큰을 모르면 0 원이 아니라 「비용 미확정」 으로 남습니다.'
      }
    >
      <div className="form-row">
        {data.policy.globalEnabled && data.policy.workspaceEnabled ? <Chip tone="ok">켜짐</Chip> : <Chip tone="watch">꺼짐(기본)</Chip>}
        {data.policy.tariffConfigured ? <Chip tone="ok">요금표 있음</Chip> : <Chip tone="bad">요금표 없음</Chip>}
        <span className="hint">모델 {String(effective.provider ?? '-')}/{String(effective.model ?? '-')}</span>
        <span className="hint">오늘 사용(예약 포함) ${data.todayChargedUsd ?? '0'} / 하루 ${String(effective.daily_usd ?? '-')}</span>
        <span className="hint mono">판 {revision}</span>
      </div>
      {data.policy.blockers.length > 0 && (
        <div className="notice watch">
          <div className="notice-kind">켜기 전에 필요한 것</div>
          <ul>{data.policy.blockers.map((code) => <li key={code}>{blockerText(code)}</li>)}</ul>
        </div>
      )}
      {error && (
        <div className="notice bad">
          <div className="notice-kind">저장 실패</div>
          <div>{error.message}</div>
          {error.blockers.length > 0 && <ul>{error.blockers.map((code) => <li key={code}>{blockerText(code)}</li>)}</ul>}
        </div>
      )}
      <div className="card card-pad model-setting">
        <div className="form-row">
          <label className="field-label" htmlFor="pdf-scope">고칠 행</label>
          <select id="pdf-scope" className="input" value={scope}
            onChange={(e) => { setScope(e.target.value as 'workspace' | 'global'); setDraft({}); setEnabled(null) }}>
            <option value="workspace">{workspace} (워크스페이스)</option>
            {canEditGlobal && <option value="global">전역 기본값 (모든 워크스페이스 · 전체 권한 관리자만)</option>}
          </select>
          <label className="nowrap">
            <input type="checkbox" checked={effectiveEnabled} onChange={(e) => setEnabled(e.target.checked)} /> 이 행 켜기
          </label>
        </div>
        {scope === 'workspace' && <div className="hint">모델·요금표는 전역 기본값에서만 고칩니다.</div>}
        {PDF_FIELDS.filter((field) => scope === 'global' || !field.globalOnly).map((field) => (
          <div className="field" key={field.key}>
            <label className="field-label" htmlFor={`pdf-${field.key}`}>{field.label}</label>
            <input id={`pdf-${field.key}`} className="input mono"
              inputMode={field.kind === 'text' ? 'text' : 'decimal'}
              placeholder={placeholder(field.key)}
              value={draft[field.key] ?? ''}
              onChange={(e) => setDraft({ ...draft, [field.key]: e.target.value })} />
            {field.hint && <div className="hint">{field.hint}</div>}
          </div>
        ))}
        <div className="field">
          <label className="field-label" htmlFor="pdf-reason">변경 사유 (감사 기록, 필수)</label>
          <input id="pdf-reason" className="input" maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
        </div>
        <div className="form-row">
          <button className="btn btn-primary" disabled={busy || !reason.trim()} onClick={() => setConfirm(true)}>저장</button>
          <span className="hint">빈 칸은 지금 값을 그대로 둡니다.</span>
        </div>
      </div>
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>상태</th><th>파일 · 쪽</th><th>예약</th><th>실제</th><th>시각</th></tr></thead>
          <tbody>
            {data.jobs.length === 0 && <tr><td colSpan={5} className="hint">작업이 없습니다.</td></tr>}
            {data.jobs.map((job) => {
              const state = PDF_JOB_STATE[job.state] ?? { tone: 'plain' as const, text: job.state }
              return (
                <tr key={job.id}>
                  <td><Chip tone={state.tone}>{state.text}</Chip></td>
                  <td className="mono">{job.fileId} · {job.page}쪽</td>
                  <td className="mono">${job.reservedUsd ?? '-'}</td>
                  <td className="mono">{job.actualUsd !== null ? `$${job.actualUsd}` : '미확정'}</td>
                  <td>{fmt.dayClock(job.finishedAt ?? job.createdAt)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <ConfirmDialog open={confirm}
        title={scope === 'global' ? '전역 PDF 전사 정책을 바꿀까요?' : `${workspace} PDF 전사 정책을 바꿀까요?`}
        detail="저장만으로 모델이 호출되거나 소급이 시작되지는 않습니다. 실행자와 사유는 감사 기록에 남습니다."
        confirmLabel="저장" busy={busy} onCancel={() => setConfirm(false)}
        onConfirm={() => { void save().finally(() => setConfirm(false)) }} />
    </Section>
  )
}

export function ImageDescriptionPage({ onToast, workspaces, canEditGlobal = true }: { onToast: (message: string) => void; workspaces: string[]; canEditGlobal?: boolean }) {
  const [workspace, setWorkspace] = useState(workspaces[0] ?? '')
  return (
    <>
      <PageHead
        crumb="운영 · 이미지 설명"
        title="이미지 설명·텍스트 인식 (B-81)"
        note={
          '수집 때 로컬 OCR 이 먼저 끝나고, 정책이 켜진 워크스페이스의 정본 사진은 사진별 승인 없이 무료 Vision 모델이 ' +
          '검색 설명과 텍스트 인식을 보완합니다(현재 ACL·원본 해시·봇 산출물 제외·무료 quota 는 그대로 확인). ' +
          '모델 장애·무료 한도 소진이어도 원본 수집은 그대로이며 검색은 로컬 OCR 로 돌아갑니다.'
        }
      />
      <SecretsSection onToast={onToast} />
      <div className="form-row">
        <label className="field-label" htmlFor="image-ws">워크스페이스</label>
        <select id="image-ws" className="input" value={workspace} onChange={(e) => setWorkspace(e.target.value)}>
          {workspaces.map((ws) => <option key={ws} value={ws}>{ws}</option>)}
        </select>
      </div>
      {workspace ? <StatusSection workspace={workspace} /> : <div className="hint">워크스페이스를 고르세요.</div>}
      {workspace && <PdfVisionSection key={workspace} workspace={workspace} canEditGlobal={canEditGlobal} onToast={onToast} />}
    </>
  )
}
