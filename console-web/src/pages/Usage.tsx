import { useResource } from '../api/hooks'
import type { UsageSnapshot } from '../types'
import { Failed, Loading, PageHead, Section, fmt } from '../components/primitives'

interface ErrorLogContext {
  at: string
  workspace: string
}

export function Usage({
  canViewLogs,
  onOpenErrorLogs,
  showRecent = true,
}: {
  canViewLogs: boolean
  onOpenErrorLogs: (context: ErrorLogContext) => void
  showRecent?: boolean
}) {
  const res = useResource<UsageSnapshot>('/api/usage')
  if (res.loading) return <Loading what="사용량을" />
  if (res.error || !res.data)
    return (
      <Failed
        what="사용량을"
        detail={res.error?.message ?? '응답이 비어 있습니다'}
        onRetry={res.reload}
      />
    )
  const usage = res.data
  // 상한이 0 이면(워크스페이스 상한 미설정) 나누기에서 무한대가 나옵니다.
  const pct = usage.limitUsd > 0 ? (usage.spentUsd / usage.limitUsd) * 100 : 0
  const projPct =
    usage.limitUsd > 0 ? Math.min(100, (usage.projectedUsd / usage.limitUsd) * 100) : 0
  const basePct =
    usage.limitUsd > 0 ? Math.min(100, (usage.baselineUsd / usage.limitUsd) * 100) : 0
  const factor = usage.baselineUsd > 0 ? usage.spentUsd / usage.baselineUsd : 0
  const peakCalls = Math.max(1, ...usage.byHour.map((h) => h.calls))
  const peakHour = usage.byHour.find((h) => h.calls === peakCalls)?.hour ?? ''
  const modelTotal = usage.byModel.reduce((a, m) => a + m.costUsd, 0)

  // 서버가 이미 권한 범위로 좁혀서 내려 줍니다(모든 집계가 같은 행 집합에서 나옵니다).
  const rows = usage.byWorkspace
  const recent = usage.recent

  return (
    <>
      <PageHead
        crumb={`데이터 · API 사용량 · ${fmt.dayClock(usage.asOf)} 기준`}
        title="API 사용량"
        note="봇이 답변을 만들 때 드는 AI 사용료를 봅니다. 하루 상한은 모든 워크스페이스를 합쳐 계산하고, 누적 금액은 서버에 기록되어 봇을 다시 띄워도 초기화되지 않습니다."
        aside={<span className="chip flat">오늘 질문 {fmt.int(usage.callsToday)}건</span>}
      />

      <Section
        title="하루 상한 대비 사용액"
        note="사선 구간은 예상치입니다"
        lead="파란 막대가 지금까지 쓴 금액입니다. 사선 구간은 지금 속도가 유지될 때 자정까지 늘어날 예상 금액이고, 세로선은 평소 이 시각의 사용액입니다."
      >
        <div className="card card-pad">
          <div className="gauge-head">
            <div>
              <div className="crumb">지금까지 사용액</div>
              <div className="gauge-figure">
                {fmt.usd(usage.spentUsd)}
                <span className="of"> / {fmt.usd(usage.limitUsd)}</span>
              </div>
            </div>
            <div style={{ textAlign: 'right' }}>
              <div className="crumb">평소 이 시각 대비</div>
              <div className="gauge-figure" style={{ fontSize: 25 }}>
                {usage.baselineUsd > 0 ? (
                  <>
                    {factor.toFixed(1)}
                    <span className="of">배</span>
                  </>
                ) : (
                  <span className="of">비교할 기록 없음</span>
                )}
              </div>
              <div className="section-note">
                {usage.baselineUsd > 0
                  ? `최근 14일 같은 시각 평균 ${fmt.usd(usage.baselineUsd)}`
                  : '최근 14일 기록이 쌓이면 평소 대비 배수를 보여 드립니다'}
              </div>
            </div>
          </div>

          <div className="gauge-track">
            <div
              className="gauge-projected"
              style={{ left: `${pct}%`, width: `${Math.max(0, projPct - pct)}%` }}
            />
            <div
              className={`gauge-fill ${pct > 100 ? 'over' : ''}`}
              style={{ width: `${Math.min(100, pct)}%` }}
            />
            <div className="gauge-baseline" style={{ left: `${basePct}%` }} />
          </div>
          <div className="gauge-scale">
            <span>$0</span>
            <span>
              상한의 {pct.toFixed(0)}% 사용 · 자정 예상 {fmt.usd(usage.projectedUsd)}
            </span>
            <span>하루 상한 {fmt.usd(usage.limitUsd)}</span>
          </div>
        </div>
      </Section>

      <Section
        title="시간대별 질문 수"
        note="가장 많았던 시간은 진하게 표시됩니다"
        lead="질문이 특정 시간에 몰리는지, 업무 시간 밖에 호출이 있는지 확인합니다."
      >
        <div className="card card-pad">
          {usage.byHour.length === 0 && (
            <p className="field-help">오늘은 아직 질문이 없습니다.</p>
          )}
          <div className="spark">
            {usage.byHour.map((h, i) => (
              <div
                key={h.hour}
                className={`spark-bar ${h.calls === peakCalls ? 'is-peak' : ''}`}
                style={{ height: `${(h.calls / peakCalls) * 100}%`, animationDelay: `${i * 18}ms` }}
                title={`${h.hour} · 질문 ${h.calls}건 · ${fmt.usd(h.costUsd)}`}
              />
            ))}
          </div>
          {usage.byHour.length > 0 && (
            <div className="spark-scale">
              <span>{usage.byHour[0].hour}</span>
              <span>
                가장 많았던 시간 {peakHour} · {peakCalls}건
              </span>
              <span>{usage.byHour[usage.byHour.length - 1].hour}</span>
            </div>
          )}
        </div>
      </Section>

      <div className="grid grid-2" style={{ marginTop: 34 }}>
        <div>
          <div className="section-head">
            <div>
              <h2 className="section-title">워크스페이스별 사용액</h2>
              <p className="section-lead">
                워크스페이스마다 상한이 따로 있습니다. 한 곳이 몰아 써도 나머지는 계속 답합니다.
              </p>
            </div>
          </div>
          <div className="card card-pad">
            <div className="table-scroll">
              <table className="table">
                <thead>
                  <tr>
                    <th>워크스페이스</th>
                    <th style={{ width: 152 }}>상한 대비</th>
                    <th className="num">질문</th>
                    <th className="num">사용액</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((w) => {
                    const p = w.limitUsd > 0 ? (w.costUsd / w.limitUsd) * 100 : 0
                    return (
                      <tr key={w.key}>
                        <td>
                          {w.label}
                          <div className="ws-key">{w.key}</div>
                        </td>
                        <td>
                          <div className="bar-cell">
                            <div className="bar-track">
                              <div
                                className={`bar-fill ${p > 85 ? 'over' : ''}`}
                                style={{ width: `${Math.min(100, p)}%` }}
                              />
                            </div>
                            <span className="mono">
                              {w.limitUsd > 0 ? `${p.toFixed(0)}%` : '미설정'}
                            </span>
                          </div>
                        </td>
                        <td className="num">{fmt.int(w.calls)}</td>
                        <td className="num">{fmt.usd(w.costUsd)}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </div>
        </div>

        <div>
          <div className="section-head">
            <div>
              <h2 className="section-title">모델별 사용액</h2>
              <p className="section-lead">
                질문 한 건에 분류용 모델과 답변용 모델이 함께 쓰이므로, 모델 호출 수 합계는 질문
                수보다 많습니다.
              </p>
            </div>
          </div>
          <div className="card card-pad">
            <div className="table-scroll">
              <table className="table">
                <thead>
                  <tr>
                    <th>모델</th>
                    <th className="num">호출</th>
                    <th className="num">입력</th>
                    <th className="num">출력</th>
                    <th className="num">비용</th>
                  </tr>
                </thead>
                <tbody>
                  {usage.byModel.map((m) => (
                    <tr key={m.model}>
                      <td className="mono">{m.model}</td>
                      <td className="num">{fmt.int(m.calls)}</td>
                      <td className="num">{fmt.tok(m.inputTokens)}</td>
                      <td className="num">{fmt.tok(m.outputTokens)}</td>
                      <td className="num">{fmt.usd(m.costUsd)}</td>
                    </tr>
                  ))}
                  <tr>
                    <td style={{ fontWeight: 700 }}>합계</td>
                    <td className="num" />
                    <td className="num" />
                    <td className="num" />
                    <td className="num" style={{ fontWeight: 700 }}>
                      {fmt.usd(modelTotal)}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>

      {showRecent && <Section
        title="최근 질문 처리 기록"
        note="질문 내용은 표시되지 않습니다"
        lead="질문 문장은 콘솔로 내려오지 않습니다. 어떤 종류의 질문이었는지, 근거를 몇 건 찾았는지, 얼마가 들었는지만 남습니다."
      >
        <div className="card card-pad">
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr>
                  <th>시각</th>
                  <th>워크스페이스</th>
                  <th>질문 종류</th>
                  <th>처리 결과</th>
                  <th className="num">찾은 근거</th>
                  <th>모델</th>
                  <th className="num">비용</th>
                  <th className="num">소요 시간</th>
                </tr>
              </thead>
              <tbody>
                {recent.map((r, i) => {
                  const failed = r.intent === 'error' || r.reason === 'error'
                  return (
                    <tr key={i} className={failed ? 'is-error-row' : undefined}>
                      <td className="mono">{r.at}</td>
                      <td>{r.workspace}</td>
                      <td className="mono">
                        {r.intent}
                        <span style={{ color: 'var(--text-3)' }}>/{r.source}</span>
                      </td>
                      <td>
                        {failed && canViewLogs ? (
                          <button
                            className="table-link"
                            type="button"
                            onClick={() =>
                              onOpenErrorLogs({ at: r.logAt, workspace: r.workspace })
                            }
                          >
                            ERROR 로그 보기
                          </button>
                        ) : (
                          <span className="mono">{r.reason}</span>
                        )}
                      </td>
                      <td className="num">{r.hits || '—'}</td>
                      <td className="mono">{r.model}</td>
                      <td className="num">{r.costUsd ? fmt.usd(r.costUsd) : '—'}</td>
                      <td className="num">{fmt.ms(r.ms)}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      </Section>}
    </>
  )
}
