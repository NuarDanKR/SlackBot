import { useEffect, useState } from 'react'
import { ApiError, api } from '../api/client'
import { useResource } from '../api/hooks'
import { Chip, Failed, Loading, PageHead, Section, fmt } from '../components/primitives'

interface TimerPreset {
  value: string
  label: string
}

interface BatchTimer {
  unit: string
  label: string
  description: string
  enabled: boolean
  active: boolean
  state: string
  nextRun: string | null
  lastRun: string | null
  lastResult: string
  preset: string
  scheduleLabel: string
  scheduleEditable: boolean
  presets: TimerPreset[]
}

interface TimerResponse {
  timers: BatchTimer[]
}

type TimerAction = 'enable' | 'disable' | 'run' | 'schedule'

function statusChip(timer: BatchTimer) {
  if (!timer.enabled) return <Chip tone="stalled">사용 안 함</Chip>
  if (!timer.active) return <Chip tone="watch">활성화 확인 필요</Chip>
  return <Chip tone="ok">실행 중</Chip>
}

function resultChip(result: string) {
  if (result === 'success') return <Chip tone="ok">최근 실행 성공</Chip>
  if (result === 'failed') return <Chip tone="bad">최근 실행 실패</Chip>
  return <Chip tone="plain">실행 기록 없음</Chip>
}

export function BatchTimers({ onToast }: { onToast: (message: string) => void }) {
  const resource = useResource<TimerResponse>('/api/timers')
  const [timers, setTimers] = useState<BatchTimer[]>([])
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [working, setWorking] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!resource.data) return
    setTimers(resource.data.timers)
    setDrafts(
      Object.fromEntries(resource.data.timers.map((timer) => [timer.unit, timer.preset])),
    )
  }, [resource.data])

  async function apply(timer: BatchTimer, action: TimerAction, preset?: string) {
    const description =
      action === 'enable'
        ? '활성화하고 지금부터 예약 실행'
        : action === 'disable'
          ? '중지하고 자동 실행 해제'
          : action === 'run'
            ? '지금 한 번 실행'
            : `실행 주기를 ${timer.presets.find((item) => item.value === preset)?.label ?? preset}로 변경`
    if (!window.confirm(`${timer.label}: ${description}하시겠습니까?`)) return
    setWorking(`${timer.unit}:${action}`)
    setError(null)
    try {
      const result = await api.put<TimerResponse>('/api/timers/action', {
        unit: timer.unit,
        action,
        preset,
      })
      setTimers(result.timers)
      setDrafts(Object.fromEntries(result.timers.map((item) => [item.unit, item.preset])))
      onToast(`${timer.label}: ${description} 요청을 반영했습니다.`)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught))
    } finally {
      setWorking(null)
    }
  }

  if (resource.loading && !resource.data) return <Loading what="배치 상태를" />
  if (resource.error && !resource.data) {
    return <Failed what="배치 상태를" detail={resource.error.message} onRetry={resource.reload} />
  }

  const enabled = timers.filter((timer) => timer.enabled).length
  return (
    <>
      <PageHead
        crumb="운영 관리"
        title="배치 관리"
        note="TYBot의 정기 작업을 확인하고 관리합니다. 변경과 즉시 실행은 관리자 감사 기록에 남습니다."
        aside={
          <>
            <Chip tone={enabled === timers.length ? 'ok' : 'watch'}>
              사용 중 {enabled}/{timers.length}
            </Chip>
            <button className="btn btn-sm" type="button" onClick={resource.reload}>
              새로고침
            </button>
          </>
        }
      />

      {error && (
        <div className="notice bad">
          <div className="notice-kind">작업 실패</div>
          <div>
            <div className="notice-title">배치 설정을 반영하지 못했습니다</div>
            <div className="notice-detail">{error}</div>
          </div>
        </div>
      )}

      <Section
        title="TYBot 정기 작업"
        lead="사용 안 함으로 표시된 작업은 자동으로 실행되지 않습니다. 일정 동기화와 일정 DM은 정확한 알림 시각을 위해 매분 실행으로 고정합니다."
      >
        <div className="grid grid-2">
          {timers.map((timer) => {
            const busy = working?.startsWith(`${timer.unit}:`) ?? false
            const selected = drafts[timer.unit] ?? timer.preset
            return (
              <article className="card card-pad" key={timer.unit}>
                <div className="card-head">
                  <div>
                    <div className="card-title">{timer.label}</div>
                    <div className="mono hint">{timer.unit}</div>
                  </div>
                  <div className="chip-row">
                    {statusChip(timer)}
                    {resultChip(timer.lastResult)}
                  </div>
                </div>
                <p className="hint">{timer.description}</p>

                <dl className="batch-meta">
                  <div>
                    <dt>실행 주기</dt>
                    <dd>{timer.scheduleLabel}</dd>
                  </div>
                  <div>
                    <dt>다음 실행</dt>
                    <dd className="mono">
                      {timer.nextRun ? fmt.systemdTime(timer.nextRun) : '예약 없음'}
                    </dd>
                  </div>
                  <div>
                    <dt>마지막 실행</dt>
                    <dd className="mono">
                      {timer.lastRun ? fmt.systemdTime(timer.lastRun) : '기록 없음'}
                    </dd>
                  </div>
                </dl>

                {timer.scheduleEditable && (
                  <div className="form-row" style={{ marginTop: 18 }}>
                    <select
                      className="input"
                      style={{ flex: 1 }}
                      value={selected}
                      disabled={busy}
                      onChange={(event) =>
                        setDrafts({ ...drafts, [timer.unit]: event.target.value })
                      }
                    >
                      {timer.presets.map((preset) => (
                        <option value={preset.value} key={preset.value}>{preset.label}</option>
                      ))}
                    </select>
                    <button
                      className="btn btn-sm"
                      type="button"
                      disabled={busy || selected === timer.preset}
                      onClick={() => apply(timer, 'schedule', selected)}
                    >
                      주기 적용
                    </button>
                  </div>
                )}

                <div className="form-row" style={{ marginTop: 14 }}>
                  {timer.enabled ? (
                    <button className="btn btn-sm btn-danger" type="button" disabled={busy} onClick={() => apply(timer, 'disable')}>
                      사용 중지
                    </button>
                  ) : (
                    <button className="btn btn-sm btn-primary" type="button" disabled={busy} onClick={() => apply(timer, 'enable')}>
                      사용 시작
                    </button>
                  )}
                  <button className="btn btn-sm" type="button" disabled={busy} onClick={() => apply(timer, 'run')}>
                    지금 실행
                  </button>
                </div>
              </article>
            )
          })}
        </div>
      </Section>
    </>
  )
}
