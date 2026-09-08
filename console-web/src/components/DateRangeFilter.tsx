import { useEffect, useState } from 'react'
import { withQuery } from '../navigation'

function shift(date: string, days: number) {
  const value = new Date(`${date}T12:00:00+09:00`)
  value.setDate(value.getDate() + days)
  return value.toLocaleDateString('sv-SE', { timeZone: 'Asia/Seoul' })
}

export function periodLabel(start: string, end: string) {
  if (start === end) return start.replace(/-/g, '.')
  return `${start.replace(/-/g, '.')} ~ ${end.replace(/-/g, '.')}`
}

export function DateRangeFilter({
  path,
  query,
  navigate,
  defaultStart,
  defaultEnd,
  today,
  preserve = {},
}: {
  path: string
  query: URLSearchParams
  navigate: (path: string) => void
  defaultStart: string
  defaultEnd: string
  today: string
  preserve?: Record<string, string | null | undefined>
}) {
  const selectedStart = query.get('start') ?? defaultStart
  const selectedEnd = query.get('end') ?? defaultEnd
  const [start, setStart] = useState(selectedStart)
  const [end, setEnd] = useState(selectedEnd)
  useEffect(() => { setStart(selectedStart); setEnd(selectedEnd) }, [selectedStart, selectedEnd])

  function move(nextStart: string, nextEnd: string) {
    navigate(withQuery(path, { ...preserve, start: nextStart, end: nextEnd }))
  }

  return <form className="period-filter" onSubmit={(event) => { event.preventDefault(); move(start, end) }}>
    <div className="period-presets" aria-label="조회 기간 빠른 선택">
      <button className="btn btn-sm" type="button" onClick={() => move(today, today)}>오늘</button>
      <button className="btn btn-sm" type="button" onClick={() => move(shift(today, -6), today)}>최근 7일</button>
      <button className="btn btn-sm" type="button" onClick={() => move(`${today.slice(0, 8)}01`, today)}>이번 달</button>
    </div>
    <div className="field"><label className="field-label" htmlFor={`${path}-start`}>시작일</label><input id={`${path}-start`} className="input" type="date" max={today} value={start} onChange={(event) => setStart(event.target.value)} /></div>
    <div className="field"><label className="field-label" htmlFor={`${path}-end`}>종료일</label><input id={`${path}-end`} className="input" type="date" min={start} max={today} value={end} onChange={(event) => setEnd(event.target.value)} /></div>
    <div className="filter-actions"><button className="btn btn-primary btn-sm" type="submit" disabled={!start || !end || start > end}>기간 적용</button></div>
  </form>
}
