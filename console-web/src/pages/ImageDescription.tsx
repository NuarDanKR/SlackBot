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
  verificationRevision?: string
  updatedBy?: string
  updatedAt?: string | null
}

/** B-101 재검증 전에 보는 것. 비용 가드가 막은 작업과 다시 돌 워크스페이스. */
interface ReverifyInspect {
  provider: string
  model: string
  zeroCostVerified: boolean
  quotaState: string
  verificationRevision: string
  verifiedAt: string | null
  updatedBy: string
  updatedAt: string | null
  disabledByGuard: boolean
  supportsVision: boolean
  credentialReady: boolean
  chargeSeen: boolean
  costJobs: { createdAt: string; workspace: string; errorCode: string; actualUsd: string | null }[]
  affectedWorkspaces: { workspace: string; enabled: boolean }[]
}

const REVERIFY_PATH = '/api/archiver/image-description/catalog/reverify'

/**
 * B-101 — 비용 가드(`runtime-cost-guard`)가 끈 무료 카탈로그를 다시 켭니다.
 * 켜면 막혀 있던 이미지가 다음 15분 배치에서 한꺼번에 나가므로, 범위와 원인을 먼저 보여 줍니다.
 */
function CatalogReverifyPanel({ row, onClose, onDone, onToast }: {
  row: CatalogRow
  onClose: () => void
  onDone: () => void
  onToast: (message: string) => void
}) {
  const query = `${REVERIFY_PATH}?provider=${encodeURIComponent(row.provider)}&model=${encodeURIComponent(row.model)}`
  const info = useResource<ReverifyInspect>(query)
  const [quotaState, setQuotaState] = useState<'unlimited' | 'available'>('unlimited')
  const [remaining, setRemaining] = useState('')
  const [reason, setReason] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const [asking, setAsking] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  if (info.loading && !info.data) return <Loading what="재검증 정보를" />
  if (info.error && !info.data) return <Failed what="재검증 정보를" detail={info.error.message} onRetry={info.reload} />
  const data = info.data
  if (!data) return null

  const remainingNumber = Number(remaining)
  const quotaOk = quotaState === 'unlimited' || (Number.isInteger(remainingNumber) && remainingNumber > 0)
  const blocked = !data.supportsVision || !data.credentialReady
  const ready = !blocked && quotaOk && confirmed && reason.trim().length > 0 && !busy
  const running = data.affectedWorkspaces.filter((w) => w.enabled)
  const idle = data.affectedWorkspaces.filter((w) => !w.enabled)
  const runningText = running.length ? running.map((w) => w.workspace).join(', ') : '없음'

  async function save(target: ReverifyInspect) {
    setBusy(true)
    setError(null)
    try {
      await api.post(REVERIFY_PATH, {
        provider: target.provider,
        model: target.model,
        expectedRevision: target.verificationRevision,
        quotaState,
        quotaRemaining: quotaState === 'available' ? remainingNumber : null,
        confirmNoCharge: confirmed,
        reason: reason.trim(),
      })
      onToast('무료 확인을 다시 적었습니다. 다음 15분 배치부터 Vision 이 다시 돕니다.')
      onDone()
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught))
    } finally {
      setBusy(false)
      setAsking(false)
    }
  }

  return (
    <div className="card card-pad model-setting">
      <div className="form-row">
        <strong className="mono">{data.provider}/{data.model}</strong>
        {data.zeroCostVerified ? <Chip tone="ok">무료 확인됨</Chip> : <Chip tone="bad">무료 미확인 — 호출 막힘</Chip>}
        <button className="btn btn-sm" onClick={onClose}>닫기</button>
      </div>
      <p className="hint">
        마지막 변경: {data.updatedAt ? fmt.dayClock(data.updatedAt) : '-'} · <span className="mono">{data.updatedBy}</span>
        {' · 검증 판 '}<span className="mono">{data.verificationRevision || '-'}</span>
      </p>
      {data.disabledByGuard && (
        <div className="notice warn"><div>
          <div className="notice-title">비용 가드가 자동으로 껐습니다</div>
          <div className="notice-detail">
            Vision 호출 한 건이 「비용 확인 불가」 또는 「과금」 으로 끝나면, 같은 모델이 계속 과금되지 않게 카탈로그를 닫습니다.
            타임아웃·전송 오류처럼 응답이 아예 없던 경우는 과금 근거가 아닙니다(2026-11-26 배포 이후로는 그것으로 닫지 않습니다).
          </div>
        </div></div>
      )}
      {data.chargeSeen && (
        <div className="notice bad"><div>
          <div className="notice-title">실제 과금 기록이 있습니다</div>
          <div className="notice-detail">아래 작업에 금액이 찍혀 있습니다. 요금제·무료 한도를 먼저 확인하지 않고 다시 켜면 같은 과금이 반복됩니다.</div>
        </div></div>
      )}
      {blocked && (
        <div className="notice bad"><div>
          <div className="notice-title">다시 켜도 돌지 않습니다</div>
          <div className="notice-detail">
            {!data.supportsVision && 'Vision 지원이 확인되지 않은 모델입니다. '}
            {!data.credentialReady && 'Vision 전용 자격이 준비되지 않았습니다. '}
            이 칸은 재검증으로 바뀌지 않습니다.
          </div>
        </div></div>
      )}
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>비용 가드가 막은 작업</th><th>워크스페이스</th><th>사유</th><th>금액(USD)</th></tr></thead>
          <tbody>
            {data.costJobs.length === 0 && <tr><td colSpan={4} className="hint">기록이 없습니다.</td></tr>}
            {data.costJobs.map((job) => (
              <tr key={`${job.createdAt}/${job.workspace}`}>
                <td>{fmt.dayClock(job.createdAt)}</td>
                <td>{job.workspace}</td>
                <td className="mono">{job.errorCode || '-'}</td>
                <td className="mono">{job.actualUsd ?? '영수증 없음'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="hint">
        다시 켜면 이 모델을 쓰는 워크스페이스에서 막혀 있던 이미지가 <strong>다음 15분 배치에서 한꺼번에</strong> 나가고,
        각 이미지 스레드의 상태 댓글이 고쳐집니다. 정책이 켜진 곳: {runningText}
        {idle.length > 0 && ` (이 모델이지만 정책이 꺼진 곳: ${idle.map((w) => w.workspace).join(', ')})`}
      </p>
      <div className="field">
        <label className="field-label" htmlFor="reverify-quota">무료 한도</label>
        <select id="reverify-quota" className="input" value={quotaState}
          onChange={(e) => setQuotaState(e.target.value === 'available' ? 'available' : 'unlimited')}>
          <option value="unlimited">무제한(오너 승인)</option>
          <option value="available">남은 횟수 지정</option>
        </select>
      </div>
      {quotaState === 'available' && (
        <div className="field">
          <label className="field-label" htmlFor="reverify-remaining">남은 무료 호출 수</label>
          <input id="reverify-remaining" className="input mono" inputMode="numeric" value={remaining}
            onChange={(e) => setRemaining(e.target.value.replace(/[^0-9]/g, ''))} />
        </div>
      )}
      <div className="field">
        <label className="field-label" htmlFor="reverify-reason">사유 (감사 기록, 필수)</label>
        <input id="reverify-reason" className="input" maxLength={500} value={reason}
          placeholder="예: 11-24 타임아웃 오판 — Google 콘솔에서 과금 0 확인"
          onChange={(e) => setReason(e.target.value)} />
      </div>
      <label className="form-row">
        <input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />
        <span>프로바이더 콘솔에서 이 모델의 과금이 없음(또는 무료 한도 안)을 직접 확인했습니다.</span>
      </label>
      {error && <div className="notice bad"><div><div className="notice-title">재검증하지 못했습니다</div><div className="notice-detail">{error}</div></div></div>}
      <div className="form-row">
        <button className="btn btn-primary" disabled={!ready} onClick={() => setAsking(true)}>무료 확인 다시 적기</button>
      </div>
      <ConfirmDialog open={asking} danger
        title={`${data.provider}/${data.model} 을 다시 켤까요?`}
        detail={`다음 15분 배치에서 막혀 있던 이미지가 한꺼번에 이 모델로 나갑니다(정책이 켜진 곳: ${runningText}). 실행자와 사유는 감사 기록에 남습니다.`}
        confirmLabel="다시 켜기" busy={busy} onCancel={() => setAsking(false)}
        onConfirm={() => void save(data)} />
    </div>
  )
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

/** B-100 이미지당 Slack 상태 댓글 하나. 본문·파일명은 오지 않는다. */
interface NoticeRow {
  channelId: string
  messageTs: string
  fileId: string
  phase: string
  jobId: string
  deliveryState: string
  version: number
  deliveredVersion: number
  attempts: number
  lastAttemptAt: string | null
  errorCode: string
  hasReply: boolean
  resendable: boolean
  slackEligible: boolean
}

const NOTICE_PHASE: Record<string, string> = {
  accepted: '접수',
  processing: '처리 중',
  completed: '인식 완료',
  uncertain: '확인 필요',
  deferred: '보류(재시도 예정)',
  failed: '처리 안 됨',
}

const NOTICE_DELIVERY: Record<string, { tone: 'ok' | 'watch' | 'bad' | 'plain'; text: string }> = {
  delivered: { tone: 'ok', text: 'Slack 반영' },
  pending: { tone: 'plain', text: '보낼 차례' },
  retrying: { tone: 'watch', text: '재시도 중' },
  failed_delivery: { tone: 'bad', text: '전달 실패' },
  not_announced: { tone: 'plain', text: '알리지 않음(소급·과거 이미지)' },
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
  notices?: NoticeRow[]
  noticesError?: string
  counts: { activeApprovals: number; activeClearances: number }
}

function NoticeTable({ workspace, rows, error, onToast, reload }: {
  workspace: string
  rows: NoticeRow[]
  error: string
  onToast: (message: string) => void
  reload: () => void
}) {
  const [busy, setBusy] = useState('')
  const resend = async (row: NoticeRow) => {
    const id = `${row.channelId}/${row.messageTs}/${row.fileId}`
    setBusy(id)
    try {
      await api.post('/api/archiver/image-description/notices/resend', {
        workspace,
        channelId: row.channelId,
        messageTs: row.messageTs,
        fileId: row.fileId,
      })
      onToast('다시 보낼 대기열에 넣었습니다. 15초 안에 Archiver 가 보냅니다.')
      reload()
    } catch (e) {
      onToast(e instanceof ApiError ? e.message : '다시 보내지 못했습니다.')
    } finally {
      setBusy('')
    }
  }
  return (
    <div className="table-wrap">
      <p className="hint">
        Slack 상태 댓글 — 이미지마다 댓글 하나를 고쳐 가며 보여 줍니다. 다시 보내기는 댓글만 보내고 Vision 은 다시 부르지 않습니다.
      </p>
      {error && <p className="hint">상태 댓글 표를 읽지 못했습니다({error}). 스키마 적용 여부를 확인하세요.</p>}
      <table className="table">
        <thead><tr><th>처리 단계</th><th>Slack</th><th>채널·메시지·파일</th><th>시도</th><th>오류 코드</th><th>마지막 시도</th><th /></tr></thead>
        <tbody>
          {rows.length === 0 && !error && <tr><td colSpan={7} className="hint">상태 댓글이 없습니다.</td></tr>}
          {rows.map((row) => {
            const id = `${row.channelId}/${row.messageTs}/${row.fileId}`
            const delivery = NOTICE_DELIVERY[row.deliveryState] ?? { tone: 'plain' as const, text: row.deliveryState }
            return (
              <tr key={id}>
                <td>{NOTICE_PHASE[row.phase] ?? row.phase}</td>
                <td><Chip tone={delivery.tone}>{delivery.text}</Chip></td>
                <td className="mono">{id}</td>
                <td>{row.attempts}</td>
                <td className="mono">{row.errorCode || '-'}</td>
                <td>{row.lastAttemptAt ? fmt.dayClock(row.lastAttemptAt) : '-'}</td>
                <td>
                  {row.resendable && (
                    <button className="btn btn-sm" disabled={busy === id} onClick={() => void resend(row)}>다시 보내기</button>
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
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

function StatusSection({ workspace, onToast }: { workspace: string; onToast: (message: string) => void }) {
  const snapshot = useResource<Snapshot>(`/api/archiver/image-description/policy?workspace=${encodeURIComponent(workspace)}`)
  const [reverify, setReverify] = useState<CatalogRow | null>(null)
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
          <thead><tr><th>모델</th><th>Vision</th><th>무료 확인</th><th>자격</th><th>무료 한도</th><th>마지막 변경</th><th /></tr></thead>
          <tbody>
            {data.catalog.length === 0 && <tr><td colSpan={7} className="hint">카탈로그가 비어 있습니다.</td></tr>}
            {data.catalog.map((row) => (
              <tr key={`${row.provider}/${row.model}`}>
                <td className="mono">{row.provider}/{row.model}</td>
                <td>{row.supportsVision ? '예' : '아니오'}</td>
                <td>{row.zeroCostVerified ? <Chip tone="ok">확인</Chip> : <Chip tone="bad">미확인</Chip>}</td>
                <td>{row.credentialReady ? '준비' : '없음'}</td>
                <td>{row.quotaState}{row.quotaRemaining !== null ? ` · ${row.quotaRemaining}` : ''}</td>
                <td>
                  {row.updatedBy === 'runtime-cost-guard'
                    ? <Chip tone="bad">비용 가드가 닫음</Chip>
                    : <span className="mono">{row.updatedBy || '-'}</span>}
                </td>
                <td className="nowrap">
                  <button className="btn btn-sm" onClick={() => setReverify(row)}>
                    {row.zeroCostVerified ? '재검증' : '다시 켜기'}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {reverify && (
        <CatalogReverifyPanel key={`${reverify.provider}/${reverify.model}`} row={reverify} onToast={onToast}
          onClose={() => setReverify(null)}
          onDone={() => { setReverify(null); snapshot.reload() }} />
      )}
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
      <NoticeTable
        workspace={workspace}
        rows={data.notices ?? []}
        error={data.noticesError ?? ''}
        onToast={onToast}
        reload={snapshot.reload}
      />
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
      {workspace ? <StatusSection workspace={workspace} onToast={onToast} /> : <div className="hint">워크스페이스를 고르세요.</div>}
      {workspace && <PdfVisionSection key={workspace} workspace={workspace} canEditGlobal={canEditGlobal} onToast={onToast} />}
    </>
  )
}
