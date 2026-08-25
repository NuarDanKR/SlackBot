import type { ReactNode } from 'react'
import type { Health } from '../types'

export function Chip({
  tone,
  children,
}: {
  tone: Health | 'plain' | 'info' | 'bad' | 'brand'
  children: ReactNode
}) {
  return <span className={`chip ${tone === 'plain' ? '' : tone}`}>{children}</span>
}

export function healthChip(h: Health) {
  if (h === 'ok') return <Chip tone="ok">수집 중</Chip>
  if (h === 'watch') return <Chip tone="watch">확인 필요</Chip>
  return <Chip tone="stalled">수집 멈춤</Chip>
}

export function Metric({ k, v, unit }: { k: string; v: string; unit?: string }) {
  return (
    <div>
      <div className="metric-k">{k}</div>
      <div className="metric-v">
        {v}
        {unit ? <span className="unit">{unit}</span> : null}
      </div>
    </div>
  )
}

/** 페이지 머리 — 제목은 메뉴 이름과 같게 둡니다. 설명은 그 아래 문장으로 풀어 씁니다. */
export function PageHead({
  crumb,
  title,
  note,
  aside,
}: {
  crumb: string
  title: string
  note?: string
  aside?: ReactNode
}) {
  return (
    <header className="page-head">
      <div>
        <div className="crumb">{crumb}</div>
        <h1 className="page-title">{title}</h1>
        {note ? <p className="page-note">{note}</p> : null}
      </div>
      {aside ? <div className="head-aside">{aside}</div> : null}
    </header>
  )
}

export function Section({
  title,
  note,
  lead,
  aside,
  children,
}: {
  title: string
  note?: string
  /** 이 구역이 무엇인지 한두 문장으로 설명합니다. */
  lead?: string
  aside?: ReactNode
  children: ReactNode
}) {
  return (
    <section className="section">
      <div className="section-head">
        <div>
          <h2 className="section-title">{title}</h2>
          {lead ? <p className="section-lead">{lead}</p> : null}
        </div>
        {aside ?? (note ? <span className="section-note">{note}</span> : null)}
      </div>
      {children}
    </section>
  )
}

export function Empty({ title, note }: { title: string; note: string }) {
  return (
    <div className="empty">
      <div className="empty-title">{title}</div>
      <p className="empty-note">{note}</p>
    </div>
  )
}

/** 수치 표기는 한 곳에서만 만듭니다 — 화면마다 자릿수가 달라지지 않게 합니다. */
export const fmt = {
  int: (n: number) => n.toLocaleString('ko-KR'),
  usd: (n: number) => `$${n.toFixed(n < 1 ? 3 : 2)}`,
  tok: (n: number) => (n >= 1000 ? `${Math.round(n / 1000)}k` : String(n)),
  ms: (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(1)}초` : `${n}ms`),
  kb: (n: number) => `${Math.max(1, Math.round(n / 1024)).toLocaleString('ko-KR')}KB`,
  clock: (iso: string) => iso.slice(11, 16),
  dayClock: (iso: string) => `${iso.slice(5, 10).replace('-', '.')} ${iso.slice(11, 16)}`,
  day: (iso: string) => iso.slice(0, 10).replace(/-/g, '.'),
}

/** 화면 기준 시각. 목데이터라 고정값입니다 — 배선할 때 서버 시각으로 바꿉니다. */
export const NOW = '2026-08-21T14:30:00+09:00'

export function sinceHours(iso: string | null): number {
  if (!iso) return Infinity
  return (Date.parse(NOW) - Date.parse(iso)) / 3_600_000
}

export function agoLabel(iso: string | null): string {
  if (!iso) return '수집 기록 없음'
  const h = sinceHours(iso)
  if (h < 1) return `${Math.max(1, Math.round(h * 60))}분 전`
  if (h < 24) return `${Math.round(h)}시간 전`
  return `${Math.floor(h / 24)}일 전`
}

/** 데이터를 불러오는 동안 자리를 지킵니다. 화면이 통째로 사라졌다 나타나지 않게. */
export function Loading({ what }: { what: string }) {
  return (
    <div className="empty">
      <div className="empty-title">{what} 불러오는 중입니다</div>
      <p className="empty-note">잠시만 기다려 주세요.</p>
    </div>
  )
}

/** 실패했을 때. 무엇이 잘못됐고 무엇을 하면 되는지 함께 적습니다. */
export function Failed({
  what,
  detail,
  onRetry,
}: {
  what: string
  detail: string
  onRetry?: () => void
}) {
  return (
    <div className="notice bad">
      <div className="notice-kind">불러오기 실패</div>
      <div>
        <div className="notice-title">{what} 불러오지 못했습니다</div>
        <div className="notice-detail">{detail}</div>
        <div className="notice-detail">
          콘솔 서버가 떠 있는지, 사내 VPN 에 연결되어 있는지 확인해 주세요.
        </div>
      </div>
      {onRetry && (
        <div className="notice-actions">
          <button className="btn btn-sm" onClick={onRetry}>
            다시 시도
          </button>
        </div>
      )}
    </div>
  )
}

/** 아직 서버에 붙지 않은 구역임을 밝힙니다. 진짜 데이터와 섞여 보이지 않게. */
export function MockBadge() {
  return (
    <span className="chip watch" title="아직 서버 API 가 없어 예시 값을 보여 줍니다">
      예시 데이터
    </span>
  )
}
