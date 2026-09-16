import { useResource } from '../api/hooks'
import { Chip, Empty, Failed, Loading, Metric, PageHead, Section, fmt } from '../components/primitives'

// 요약 검토 회차 현황(B-50). 설계: docs/design/summary-review-canvas.md §9
//
// **본문과 정정사항을 표시하지 않는다.** 정정은 검토자가 쓴 판단이고, 관리자
// 화면이 그것을 띄우면 다음 검토가 그 문장에 끌린다. 여기 있는 것은 상태와
// 수치, 그리고 Canvas 로 가는 링크뿐이다.

type Round = {
  id: string
  workspace: string
  channelId: string
  channelName: string
  reviewDate: string
  state: string
  errorCode: string
  canvasUrl: string
  candidates: number
  decided: number
  sent: number
  failed: number
  createdAt: string
}

const STATE_LABEL: Record<string, string> = {
  creating: '생성 중',
  ready: '검토 대기',
  partial: '일부 결정',
  completed: '결정 완료',
  failed: 'Canvas 실패',
  ambiguous: '확인 필요',
}

const STATE_TONE: Record<string, 'ok' | 'watch' | 'bad' | 'plain'> = {
  creating: 'plain',
  ready: 'plain',
  partial: 'watch',
  completed: 'ok',
  failed: 'watch',
  // 자동으로 다시 만들지 않는다. 사람이 봐야 다음으로 간다.
  ambiguous: 'bad',
}

export function SummaryReviews() {
  const res = useResource<{ rounds: Round[]; ambiguous: Round[] }>('/api/summary-review/rounds')
  if (res.loading) return <Loading what="요약 검토 회차를" />
  if (res.error || !res.data) {
    return <Failed what="요약 검토 회차를" detail={res.error?.message ?? '응답이 없습니다.'} onRetry={res.reload} />
  }
  const rounds = res.data.rounds
  const stuck = res.data.ambiguous
  const open = rounds.filter((r) => r.state === 'ready' || r.state === 'partial').length
  const failed = rounds.filter((r) => r.state === 'failed').length

  return (
    <>
      <PageHead
        crumb="수집 · 요약 검토"
        title="요약 검토 현황"
        note="채널·검토일별 회차의 발송과 결정 상태입니다. 후보 본문과 정정사항은 표시하지 않습니다."
      />
      {stuck.length > 0 && (
        <div className="notice bad">
          <div>
            <div className="notice-title">확인이 필요한 회차 {stuck.length}건</div>
            <div className="notice-detail">
              Canvas 가 만들어졌는지 확실하지 않아 멈춰 있습니다. 중복 문서를 막기 위해 자동으로
              다시 만들지 않습니다. Slack 에서 해당 채널의 Canvas 를 확인한 뒤 정리해 주세요.
            </div>
          </div>
        </div>
      )}
      <Section title="회차 요약">
        <div className="metrics overview-metrics">
          <Metric k="전체 회차" v={fmt.int(rounds.length)} unit="건" />
          <Metric k="검토 진행 중" v={fmt.int(open)} unit="건" />
          <Metric k="Canvas 실패" v={fmt.int(failed)} unit="건" />
          <Metric k="확인 필요" v={fmt.int(stuck.length)} unit="건" />
        </div>
      </Section>
      <Section title="회차 목록" note={`${rounds.length}건`}>
        {rounds.length === 0 ? (
          <Empty title="아직 회차가 없습니다." note="검토자를 정한 채널에서 설정 시각이 지나면 생깁니다." />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>검토일</th>
                  <th>워크스페이스</th>
                  <th>채널</th>
                  <th>상태</th>
                  <th>후보</th>
                  <th>결정</th>
                  <th>발송</th>
                  <th>문서</th>
                </tr>
              </thead>
              <tbody>
                {rounds.map((row) => (
                  <tr key={row.id}>
                    <td>{row.reviewDate}</td>
                    <td>{row.workspace}</td>
                    <td>{row.channelName || row.channelId}</td>
                    <td>
                      <Chip tone={STATE_TONE[row.state] ?? 'plain'}>{STATE_LABEL[row.state] ?? row.state}</Chip>
                      {row.errorCode && <span className="hint mono"> {row.errorCode}</span>}
                    </td>
                    <td>{fmt.int(row.candidates)}</td>
                    <td>
                      {fmt.int(row.decided)} / {fmt.int(row.candidates)}
                    </td>
                    <td>
                      {fmt.int(row.sent)}
                      {row.failed > 0 && <span className="hint"> · 실패 {fmt.int(row.failed)}</span>}
                    </td>
                    <td>
                      {row.canvasUrl ? (
                        <a className="table-link" href={row.canvasUrl} target="_blank" rel="noreferrer">
                          Canvas 열기
                        </a>
                      ) : (
                        '없음'
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>
      <Section title="이 화면이 보여 주지 않는 것" lead="검토 내용은 검토자와 후보 기록에만 남습니다.">
        <ul className="basis-list">
          <li>
            <strong>후보 본문과 근거 원문</strong>: Canvas 에서 읽습니다. 콘솔에 복제하지 않습니다.
          </li>
          <li>
            <strong>정정사항</strong>: 반려한 검토자가 쓴 판단이며 피드백 기록에만 남습니다.
          </li>
          <li>
            <strong>Canvas 링크</strong>: 그 회차의 수신자만 열 수 있습니다. 채널 전체에는 공유하지 않습니다.
          </li>
        </ul>
      </Section>
    </>
  )
}
