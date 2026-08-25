/** 수집 추이 그래프.
 *
 * 하루에 쌓인 원문 줄 수를 한 칸으로 두고 최근 30일을 나란히 보여 줍니다.
 * 수집이 없던 날은 채우지 않고 점선 칸으로 남깁니다.
 *
 * 총 줄 수만 크게 보여 주면 "최근에 멈췄다"는 사실이 드러나지 않습니다. 수집이 멈춘 봇은
 * 오류 없이 옛 자료로 답하기 때문에, 최근 며칠이 비었는지가 가장 중요한 정보입니다.
 */
import type { DailyCourse, WorkspaceStatus } from '../types'
import { agoLabel, fmt, sinceHours } from './primitives'

/** 농도 4단계 — 같은 워크스페이스 안에서의 상대량입니다. 팀끼리 비교하는 값이 아닙니다. */
function level(lines: number, peak: number): string {
  if (lines === 0) return 'is-void'
  const r = lines / peak
  if (r > 0.72) return 'q4'
  if (r > 0.45) return 'q3'
  if (r > 0.2) return 'q2'
  return 'q1'
}

function Courses({ courses, label }: { courses: DailyCourse[]; label: string }) {
  const peak = Math.max(1, ...courses.map((c) => c.lines))
  return (
    <div className="courses" role="img" aria-label={`${label} 최근 ${courses.length}일 수집 추이`}>
      {courses.map((c, i) => (
        <div
          key={c.date}
          className={`course ${level(c.lines, peak)}`}
          style={{
            height: c.lines === 0 ? '100%' : `${Math.max(16, (c.lines / peak) * 100)}%`,
            animationDelay: `${i * 9}ms`,
          }}
          title={
            c.lines === 0
              ? `${c.date} · 수집된 대화가 없습니다`
              : `${c.date} · 원문 ${fmt.int(c.lines)}줄`
          }
        />
      ))}
    </div>
  )
}

export function Strata({ items }: { items: WorkspaceStatus[] }) {
  return (
    <div className="strata">
      <div className="strata-legend">
        <span>최근 30일 · 한 칸이 하루</span>
        <span className="legend-item">
          <span className="legend-swatch" />
          쌓인 대화량
        </span>
        <span className="legend-item">
          <span className="legend-swatch is-void" />
          수집 없음
        </span>
        <span style={{ marginLeft: 'auto' }}>진한 칸일수록 그날 많이 쌓였습니다</span>
      </div>

      {items.map((w) => {
        const late = sinceHours(w.lastIngestedAt) > 24
        return (
          <div className="band" key={w.key}>
            <div className="band-name">
              <div className="band-label">{w.label}</div>
              <div className="band-key">
                {w.key}
                {w.role === 'root' ? <span className="root-mark"> · 상위</span> : null}
              </div>
            </div>
            <Courses courses={w.courses} label={w.label} />
            <div className="band-figure">
              <div className="band-lines">{fmt.int(w.rawLines)}</div>
              <div className={`band-when ${late ? 'is-late' : ''}`}>
                {agoLabel(w.lastIngestedAt)}
              </div>
            </div>
          </div>
        )
      })}
    </div>
  )
}
