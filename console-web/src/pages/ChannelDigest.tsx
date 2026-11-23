import { useState } from 'react'
import { ApiError, api } from '../api/client'
import { useResource } from '../api/hooks'
import { Chip, Failed, Loading, Section } from '../components/primitives'

/**
 * 운영 > 검색 다이제스트 (B-93 2단계).
 *
 * 다이제스트는 검색 길잡이라 **본문을 보여 주지 않는다** — 채널별 건수·날짜만.
 * 소급은 비용이 들어서 채널·기간을 고르고 분량을 먼저 센 뒤에만 실행 버튼이 열린다.
 */

interface ChannelRow {
  workspace: string
  channelId: string
  channelName: string
  current: number
  stale: number
  latestDay: string | null
  latestCreated: string | null
}

interface StatusView {
  bot: string
  channels: ChannelRow[]
  error: string
  maxCalls?: number
  maxRangeDays?: number
}

interface Count {
  since: string
  until: string
  channels: number
  days_seen: number
  unchanged: number
  calls: number
  est_input_tokens: number
  started?: boolean
}

function daysAgo(n: number): string {
  const d = new Date(Date.now() + 9 * 3600_000 - n * 86400_000)
  return d.toISOString().slice(0, 10)
}

export function ChannelDigestPage({ onToast }: { onToast: (message: string) => void }) {
  const status = useResource<StatusView>('/api/channel-digest')
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [since, setSince] = useState(daysAgo(30))
  const [until, setUntil] = useState(daysAgo(1))
  const [count, setCount] = useState<Count | null>(null)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)

  if (status.loading) return <Loading what="검색 다이제스트" />
  if (status.error || !status.data) {
    return <Failed what="검색 다이제스트" detail={status.error?.message ?? ''} onRetry={status.reload} />
  }
  const view = status.data
  const range = { channels: [...picked], since, until }

  const toggle = (id: string) => {
    const next = new Set(picked)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    setPicked(next)
    setCount(null)
  }

  const preview = async () => {
    setBusy(true)
    try {
      setCount(await api.post<Count>('/api/channel-digest/preview', range))
    } catch (err) {
      onToast(err instanceof ApiError ? err.message : '분량을 세지 못했습니다')
    } finally {
      setBusy(false)
    }
  }

  const run = async () => {
    if (!count || count.calls === 0) return
    setBusy(true)
    try {
      const result = await api.post<Count>('/api/channel-digest/run', {
        ...range,
        expectedCalls: count.calls,
        maxCalls: count.calls,
        reason,
      })
      onToast(result.started ? `소급을 시작했습니다 (호출 ${result.calls}건)` : '만들 것이 없습니다')
      setCount(null)
      setReason('')
      status.reload()
    } catch (err) {
      onToast(err instanceof ApiError ? err.message : '실행하지 못했습니다')
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <Section
        title="검색 다이제스트"
        lead="채널마다 하루치 대화와 첨부를 묶은 검색 길잡이입니다. 질문 낱말이 문서와 달라도 그날 원문을 찾게 합니다. 길잡이 문장은 답변 근거로 쓰이지 않고 화면에도 보이지 않습니다."
        aside={<Chip tone="plain">대상: {view.bot} 근거 채널</Chip>}
      >
        {view.error && <p className="muted">표를 읽지 못했습니다({view.error}). 스키마 적용을 확인하세요.</p>}
        <table className="table">
          <thead>
            <tr>
              <th />
              <th>채널</th>
              <th>현재</th>
              <th>낡음</th>
              <th>최근 날짜</th>
            </tr>
          </thead>
          <tbody>
            {view.channels.map((row) => (
              <tr key={`${row.workspace}/${row.channelId}`}>
                <td>
                  <input
                    type="checkbox"
                    checked={picked.has(row.channelId)}
                    onChange={() => toggle(row.channelId)}
                    aria-label={`${row.channelName || row.channelId} 선택`}
                  />
                </td>
                <td>
                  {row.channelName || row.channelId} <span className="muted">({row.workspace})</span>
                </td>
                <td>{row.current}</td>
                <td>{row.stale > 0 ? <Chip tone="watch">{row.stale}</Chip> : 0}</td>
                <td>{row.latestDay ?? '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>

      <Section
        title="소급 만들기"
        lead="채널과 기간을 고르고 먼저 분량을 셉니다. LLM 비용이 들기 때문에 분량을 본 뒤에만 실행할 수 있습니다. 이미 만든 날 중 원문이 바뀌지 않은 날은 건너뜁니다."
      >
        <div className="form-row">
          <label>
            시작일 <input type="date" value={since} onChange={(e) => { setSince(e.target.value); setCount(null) }} />
          </label>
          <label>
            종료일 <input type="date" value={until} onChange={(e) => { setUntil(e.target.value); setCount(null) }} />
          </label>
          <button type="button" disabled={busy || picked.size === 0} onClick={preview}>
            분량 세기
          </button>
        </div>
        {count && (
          <div className="form-row">
            <p>
              {count.since} ~ {count.until} · 채널 {count.channels}개 · 원문 있는 날 {count.days_seen}일 ·
              변경 없음 {count.unchanged}일 · <strong>LLM 호출 {count.calls}건</strong> · 추정 입력 토큰{' '}
              {count.est_input_tokens.toLocaleString()}
            </p>
            <label>
              사유 <input value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} />
            </label>
            <button type="button" disabled={busy || count.calls === 0 || !reason.trim()} onClick={run}>
              {count.calls}건 실행
            </button>
          </div>
        )}
      </Section>
    </>
  )
}
