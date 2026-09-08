import { useEffect, useState } from 'react'
import { ApiError, api } from '../api/client'
import { useResource } from '../api/hooks'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { Chip, Failed, Loading, PageHead, Section, fmt } from '../components/primitives'

type RuntimeState = 'idle' | 'queued' | 'running' | 'ok' | 'failed' | 'skipped'

interface DeploymentStatus {
  state: RuntimeState
  pending: boolean
  actor: string
  before: string
  after: string
  beforeTitle: string
  afterTitle: string
  message: string
  detail: string
  requestedAt: string | null
  startedAt: string | null
  finishedAt: string | null
}

const RUNTIME_LABEL: Record<RuntimeState, string> = {
  idle: '배포 기록 없음',
  queued: '배포 대기 중',
  running: '배포 진행 중',
  ok: '최근 배포 성공',
  failed: '최근 배포 실패',
  skipped: '새 변경 없음',
}

function runtimeChip(state: RuntimeState) {
  if (state === 'ok') return <Chip tone="ok">{RUNTIME_LABEL[state]}</Chip>
  if (state === 'failed') return <Chip tone="bad">{RUNTIME_LABEL[state]}</Chip>
  if (state === 'queued' || state === 'running') return <Chip tone="watch">{RUNTIME_LABEL[state]}</Chip>
  return <Chip tone="plain">{RUNTIME_LABEL[state]}</Chip>
}

export function Deploy({ onToast }: { onToast: (message: string) => void }) {
  const runtime = useResource<DeploymentStatus>('/api/deployment')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [confirming, setConfirming] = useState(false)

  useEffect(() => {
    if (runtime.data?.state !== 'queued' && runtime.data?.state !== 'running') return
    const timer = window.setInterval(runtime.reload, 5000)
    return () => window.clearInterval(timer)
  }, [runtime.data?.state, runtime.reload])

  async function deployNow() {
    setSubmitting(true)
    setError(null)
    try {
      await api.put('/api/deployment/request', {})
      runtime.reload()
      onToast('마스터 봇 배포를 시작했습니다.')
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught))
    } finally {
      setSubmitting(false)
    }
  }

  if (runtime.loading && !runtime.data) return <Loading what="배포 상태를" />
  if (runtime.error && !runtime.data) return <Failed what="배포 상태를" detail={runtime.error.message} onRetry={runtime.reload} />
  const current = runtime.data
  const busy = current?.state === 'queued' || current?.state === 'running'

  return <>
    <PageHead crumb="운영 · 배포" title="배포 관리"
      note="관리자가 TYBot 마스터 저장소의 최신 코드를 검사하고 설치한 뒤 서비스를 재시작합니다. 전문 봇 등록과 변경 승인은 전문 봇 관리에서 처리합니다."
      aside={current ? runtimeChip(current.state) : <Chip tone="plain">상태 없음</Chip>} />

    {error && <div className="notice bad"><div className="notice-kind">처리 실패</div><div>
      <div className="notice-title">배포를 시작하지 못했습니다</div><div className="notice-detail">{error}</div>
    </div></div>}

    <Section title="배포 대상" lead="이 화면은 TYBot 마스터와 관리 콘솔만 배포합니다. 전문 봇은 등록된 어댑터와 승인된 버전을 기준으로 별도 관리합니다.">
      <div className="action-list">
        <a className="action-row action-link" href="#/manage/specialists">
          <span><strong>전문 봇 관리</strong><small>전문 봇 등록·변경 요청, 승인 결과, 계약 버전과 헬스를 확인합니다.</small></span>
          <span aria-hidden="true">→</span>
        </a>
      </div>
    </Section>

    {current && <Section title="최근 서버 배포" lead="실제 root 배포 러너의 상태와 결과입니다.">
      <div className="card card-pad"><div className="card-head"><div><div className="card-title">{RUNTIME_LABEL[current.state]}</div>
        <p className="hint">{current.message || '아직 기록된 배포 결과가 없습니다.'}</p></div>
        <div className="deploy-head-actions">{runtimeChip(current.state)}
          <button className="btn btn-primary" disabled={submitting || busy}
            onClick={() => setConfirming(true)}>{busy ? '배포 중…' : '지금 배포'}</button></div></div>
        <div className="deploy-actor"><span className="metric-label">실행 관리자</span> {current.actor || '-'}</div>
        <div className="deploy-diff">
          <div className="deploy-diff-side is-before"><div className="metric-label">배포 전</div>
            <div className="mono deploy-diff-sha">{current.before || '-'}</div>
            <div className="deploy-commit-title" title={current.beforeTitle}>{current.beforeTitle || '-'}</div></div>
          <div className="deploy-diff-arrow" aria-hidden="true">→</div>
          <div className="deploy-diff-side is-after"><div className="metric-label">배포 후</div>
            <div className="mono deploy-diff-sha">{current.after || '-'}</div>
            <div className="deploy-commit-title" title={current.afterTitle}>{current.afterTitle || '-'}</div></div>
        </div>
        {current.state === 'failed' && current.detail && <div className="deploy-failure"><div className="metric-label">실패 사유</div><pre>{current.detail}</pre></div>}
        <div className="hint" style={{ marginTop: 16 }}>요청 {current.requestedAt ? fmt.dayClock(current.requestedAt) : '-'} · 시작 {current.startedAt ? fmt.dayClock(current.startedAt) : '-'} · 완료 {current.finishedAt ? fmt.dayClock(current.finishedAt) : '-'}</div>
      </div>
    </Section>}

    <ConfirmDialog open={confirming} title="지금 마스터 봇을 배포할까요?"
      detail="서버의 전체 테스트와 fast-forward 검사를 통과한 코드만 반영되며, 실행 결과는 감사 기록에 남습니다."
      confirmLabel="지금 배포" danger busy={submitting}
      onCancel={() => setConfirming(false)}
      onConfirm={() => void deployNow().finally(() => setConfirming(false))} />
  </>
}
