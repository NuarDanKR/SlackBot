import { useEffect, useState } from 'react'
import { ApiError, api } from '../api/client'
import { useResource } from '../api/hooks'
import { Chip, Failed, Loading, PageHead, Section } from '../components/primitives'

type DeployState = 'idle' | 'queued' | 'running' | 'ok' | 'failed' | 'skipped'

interface DeploymentStatus {
  state: DeployState
  pending: boolean
  actor: string
  before: string
  after: string
  message: string
  requestedAt: string | null
  startedAt: string | null
  finishedAt: string | null
}

const STATE_LABEL: Record<DeployState, string> = {
  idle: '배포 기록 없음',
  queued: '배포 대기 중',
  running: '배포 진행 중',
  ok: '최근 배포 성공',
  failed: '최근 배포 실패',
  skipped: '새 변경 없음',
}

function stateChip(state: DeployState) {
  if (state === 'ok') return <Chip tone="ok">{STATE_LABEL[state]}</Chip>
  if (state === 'failed') return <Chip tone="bad">{STATE_LABEL[state]}</Chip>
  if (state === 'queued' || state === 'running') {
    return <Chip tone="watch">{STATE_LABEL[state]}</Chip>
  }
  return <Chip tone="plain">{STATE_LABEL[state]}</Chip>
}

export function Deploy({ onToast }: { onToast: (message: string) => void }) {
  const resource = useResource<DeploymentStatus>('/api/deployment')
  const [latest, setLatest] = useState<DeploymentStatus | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (resource.data) setLatest(resource.data)
  }, [resource.data])

  useEffect(() => {
    if (latest?.state !== 'queued' && latest?.state !== 'running') return
    const timer = window.setInterval(resource.reload, 5000)
    return () => window.clearInterval(timer)
  }, [latest?.state, resource.reload])

  async function deploy() {
    if (!window.confirm('새 커밋을 확인하고 테스트를 통과하면 운영 서버에 배포하시겠습니까?')) return
    setSubmitting(true)
    setError(null)
    try {
      const queued = await api.put<DeploymentStatus>('/api/deployment/request', {})
      setLatest(queued)
      onToast('배포를 요청했습니다. 이 화면에서 진행 결과를 확인할 수 있습니다.')
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught))
    } finally {
      setSubmitting(false)
    }
  }

  if (resource.loading && !resource.data) return <Loading what="배포 상태를" />
  if (resource.error && !resource.data) {
    return <Failed what="배포 상태를" detail={resource.error.message} onRetry={resource.reload} />
  }

  const status = latest ?? resource.data
  if (!status) return null
  const busy = status.state === 'queued' || status.state === 'running'
  return (
    <>
      <PageHead
        crumb="운영 관리 · 배포"
        title="배포 관리"
        note="서버 터미널에서 update.sh를 실행하는 대신 새 커밋 확인, 전체 테스트, 설치, TYBot 재시작을 요청합니다. 테스트가 실패하면 운영 코드는 바뀌지 않습니다."
        aside={stateChip(status.state)}
      />

      {error && (
        <div className="notice bad">
          <div className="notice-kind">요청 실패</div>
          <div>
            <div className="notice-title">배포를 시작하지 못했습니다</div>
            <div className="notice-detail">{error}</div>
          </div>
        </div>
      )}

      <Section
        title="업데이트 확인 및 배포"
        lead="브랜치와 서버 경로는 배포 서비스에 고정되어 있습니다. 이 화면에서는 임의 명령이나 배포 대상을 입력할 수 없습니다."
      >
        <div className="card card-pad">
          <div className="card-head">
            <div>
              <div className="card-title">{STATE_LABEL[status.state]}</div>
              <p className="hint">{status.message || '아직 기록된 배포 결과가 없습니다.'}</p>
            </div>
            <button
              className="btn btn-primary"
              type="button"
              disabled={busy || submitting}
              onClick={deploy}
            >
              {busy ? '배포 진행 중' : submitting ? '요청 중' : '업데이트 확인 및 배포'}
            </button>
          </div>

          <div className="metric-row" style={{ marginTop: 20 }}>
            <div>
              <div className="metric-label">요청자</div>
              <div>{status.actor || '기록 없음'}</div>
            </div>
            <div>
              <div className="metric-label">배포 전 커밋</div>
              <div className="mono">{status.before || '-'}</div>
            </div>
            <div>
              <div className="metric-label">배포 후 커밋</div>
              <div className="mono">{status.after || '-'}</div>
            </div>
          </div>
          <div className="hint" style={{ marginTop: 16 }}>
            요청 {status.requestedAt ?? '-'} · 시작 {status.startedAt ?? '-'} · 완료{' '}
            {status.finishedAt ?? '-'}
          </div>
        </div>
      </Section>
    </>
  )
}
