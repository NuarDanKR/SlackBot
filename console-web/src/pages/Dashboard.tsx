import { useResource } from '../api/hooks'
import { anomalies } from '../mock/data'
import type { ConsoleUser, WorkspaceStatus } from '../types'
import { Strata } from '../components/Strata'
import {
  Chip,
  Failed,
  Loading,
  Metric,
  MockBadge,
  PageHead,
  Section,
  agoLabel,
  fmt,
  healthChip,
} from '../components/primitives'

const KIND_LABEL: Record<string, string> = {
  spike: '사용량 급증',
  limit: '상한 임박',
  loop: '반복 호출',
  stalled: '수집 중단',
}

export function Dashboard({
  user,
  onToast,
}: {
  user: ConsoleUser
  onToast: (m: string) => void
}) {
  // 서버가 이미 권한 범위로 좁혀서 내려 줍니다. 화면에서 다시 거르지 않습니다.
  const res = useResource<{ workspaces: WorkspaceStatus[] }>('/api/status')
  const list = res.data?.workspaces ?? []
  // 이상 감지는 아직 서버 API 가 없습니다(BACKLOG B-13).
  const open = anomalies.filter(
    (a) => a.state === 'open' && (user.role === 'owner' || user.workspaces.includes(a.workspace)),
  )
  const totalLines = list.reduce((a, w) => a + w.rawLines, 0)
  const totalDocs = list.reduce((a, w) => a + w.docs, 0)
  const stalled = list.filter((w) => w.health === 'stalled')

  return (
    <>
      <PageHead
        crumb={`데이터 · 데이터 현황 · ${fmt.dayClock('2026-08-21T14:30:00+09:00')} 기준`}
        title="데이터 현황"
        note="봇은 아카이브에 쌓인 대화만 근거로 답합니다. 수집이 멈추면 오류 없이 예전 자료로 답하게 되므로, 워크스페이스마다 대화가 지금도 쌓이고 있는지 이 화면에서 확인합니다."
        aside={
          <>
            <Chip tone="ok">봇 정상 동작</Chip>
            <Chip tone="plain">
              문서 {fmt.int(totalDocs)}건 · 원문 {fmt.int(totalLines)}줄
            </Chip>
          </>
        }
      />

      {res.loading && <Loading what="수집 현황을" />}
      {res.error && (
        <div className="section">
          <Failed what="수집 현황을" detail={res.error.message} onRetry={res.reload} />
        </div>
      )}

      {open.length > 0 && (
        <Section
          title="확인이 필요한 항목"
          aside={<MockBadge />}
          lead="정상 범위를 벗어난 상태입니다. 처리하거나 확인 표시를 하면 목록에서 내려갑니다."
        >
          {open.map((a) => (
            <div className="notice warn" key={a.id} style={{ marginBottom: 10 }}>
              <div className="notice-kind">{KIND_LABEL[a.kind]}</div>
              <div>
                <div className="notice-title">{a.headline}</div>
                <div className="notice-detail">{a.detail}</div>
              </div>
              <div className="notice-actions">
                <button
                  className="btn btn-sm"
                  onClick={() => onToast(`확인 표시했습니다 — ${a.headline}`)}
                >
                  확인 표시
                </button>
              </div>
            </div>
          ))}
        </Section>
      )}

      <Section
        title="워크스페이스별 수집 추이"
        note={
          stalled.length ? `수집이 멈춘 워크스페이스 ${stalled.length}개` : '모두 정상 수집 중'
        }
        lead="한 칸이 하루입니다. 오른쪽으로 갈수록 최근이고, 점선 칸은 그날 수집된 대화가 없다는 뜻입니다."
      >
        <Strata items={list} />
      </Section>

      <Section
        title="워크스페이스 상태"
        note={`${list.length}개`}
        lead="문서 수와 원문 줄 수, 마지막 수집 시각으로 각 봇이 제대로 일하고 있는지 확인합니다."
      >
        <div className="grid grid-3">
          {list.map((w) => (
            <article className="ws-card" key={w.key}>
              <div className="ws-top">
                <div>
                  <div className="ws-name">{w.label}</div>
                  <div className="ws-key">
                    {w.key}
                    {w.role === 'root' ? ' · 상위 워크스페이스' : ''}
                  </div>
                </div>
                {healthChip(w.health)}
              </div>

              <div className="metrics">
                <Metric k="문서" v={fmt.int(w.docs)} unit="건" />
                <Metric k="원문" v={fmt.int(w.rawLines)} unit="줄" />
                <Metric k="채널" v={fmt.int(w.channels)} unit="개" />
              </div>

              <div className="ws-foot">
                <span>
                  {w.lastIngestedAt ? `마지막 수집 ${agoLabel(w.lastIngestedAt)}` : '수집 기록 없음'}
                </span>
                <span className="mono">
                  {fmt.usd(w.spendTodayUsd)} / {fmt.usd(w.limitUsd)}
                </span>
              </div>

              {w.writeProblem && (
                <div className="notice bad" style={{ padding: '11px 13px' }}>
                  <div>
                    <div className="notice-title" style={{ fontSize: 12.5 }}>
                      아카이브에 저장하지 못하고 있습니다
                    </div>
                    <div className="notice-meta">{w.writeProblem}</div>
                    <div className="notice-detail" style={{ fontSize: 12 }}>
                      봇은 응답하지만 대화가 저장되지 않는 상태입니다. 서버 저장 경로 권한을 확인해
                      주세요.
                    </div>
                  </div>
                </div>
              )}

              {!w.writeProblem && w.uninvitedChannels > 0 && (
                <p className="hint">
                  봇이 초대되지 않은 채널이 {w.uninvitedChannels}개 있습니다. 해당 채널에서{' '}
                  <code>/invite @tybot</code> 을 입력하면 그 채널 대화도 수집됩니다.
                </p>
              )}

              {w.brokenDocs > 0 && (
                <p className="hint warn">
                  형식이 맞지 않는 문서가 {w.brokenDocs}건 있습니다. 이 문서는 답변 근거로 쓰이지
                  않으니 수집 문서 열람에서 확인해 주세요.
                </p>
              )}

              {!w.realtime && (
                <p className="hint warn">
                  실시간 수집이 꺼져 있습니다. 정기 수집만으로는 채널당 하루 195건까지만 모을 수
                  있어 대화가 많은 채널은 빠질 수 있습니다.
                </p>
              )}

              {w.readable.length > 0 && (
                <p className="hint">
                  다른 워크스페이스 열람 허용: {w.readable.join(', ')}
                  {w.role === 'root'
                    ? ' — 상위 워크스페이스이므로 산하 자료를 모두 볼 수 있습니다.'
                    : ' — 상대가 공유로 표시한 문서만 볼 수 있습니다.'}
                </p>
              )}

              {!w.connected && <Chip tone="stalled">Slack 연결이 끊겼습니다</Chip>}
            </article>
          ))}
        </div>
      </Section>
    </>
  )
}
