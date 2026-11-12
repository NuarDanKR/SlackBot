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
        <span className="hint">활성 승인 {data.counts.activeApprovals} · 활성 clearance {data.counts.activeClearances}</span>
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

export function ImageDescriptionPage({ onToast, workspaces }: { onToast: (message: string) => void; workspaces: string[] }) {
  const [workspace, setWorkspace] = useState(workspaces[0] ?? '')
  return (
    <>
      <PageHead
        crumb="운영 · 이미지 설명"
        title="이미지 설명·텍스트 인식 (B-81)"
        note={
          '수집 때 로컬 OCR 이 먼저 끝나고, 승인된 사진만 무료 Vision 모델이 검색 설명과 텍스트 인식을 보완합니다. ' +
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
    </>
  )
}
