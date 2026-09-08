import { useResource } from '../api/hooks'
import type { ConsoleUser, WorkspaceStatus } from '../types'
import { Strata } from '../components/Strata'
import {
  Chip,
  Failed,
  Loading,
  Metric,
  PageHead,
  Section,
  fmt,
} from '../components/primitives'

export function Dashboard({ user, query }: { user: ConsoleUser; query?: URLSearchParams }) {
  // 서버가 이미 권한 범위로 좁혀서 내려 줍니다. 화면에서 다시 거르지 않습니다.
  const res = useResource<{ workspaces: WorkspaceStatus[] }>('/api/status')
  const workspace = query?.get('workspace') ?? ''
  const state = query?.get('state') ?? ''
  const list = (res.data?.workspaces ?? []).filter(
    (row) => (!workspace || row.key === workspace) && (!state || row.health === state),
  )
  const totalLines = list.reduce((a, w) => a + w.rawLines, 0)
  const totalDocs = list.reduce((a, w) => a + w.docs, 0)
  const stalled = list.filter((w) => w.health === 'stalled')
  const brokenDocuments = list.reduce((sum, w) => sum + w.brokenDocs, 0)
  const uninvitedChannels = list.reduce((sum, w) => sum + w.uninvitedChannels, 0)
  const writeProblems = list.filter((w) => Boolean(w.writeProblem))
  const needsAttention = list.some(
    (w) => w.health !== 'ok' || w.connected !== true || Boolean(w.writeProblem),
  )

  return (
    <>
      <PageHead
        crumb="수집 · 수집 현황"
        title="수집 현황"
        note="봇은 아카이브에 쌓인 대화만 근거로 답합니다. 수집이 멈추면 오류 없이 예전 자료로 답하게 되므로, 워크스페이스마다 대화가 지금도 쌓이고 있는지 이 화면에서 확인합니다."
        aside={
          <>
            {res.loading ? (
              <Chip tone="plain">현황 확인 중</Chip>
            ) : res.error ? (
              <Chip tone="bad">현황 조회 실패</Chip>
            ) : !list.length ? (
              <Chip tone="watch">등록된 워크스페이스 없음</Chip>
            ) : needsAttention ? (
              <Chip tone="watch">확인 필요</Chip>
            ) : (
              <Chip tone="ok">봇 정상 동작</Chip>
            )}
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

      {res.data && (
        <Section
          title="현재 수집량과 조치 항목"
          lead="전체 수집량과 중단·누락·문서 형식 문제를 한곳에서 확인합니다. 숫자를 누르면 원인을 확인할 수 있는 화면으로 이동합니다."
        >
          <div className="metrics overview-metrics">
            <Metric k="문서" v={fmt.int(totalDocs)} unit="건" />
            <Metric k="원문" v={fmt.int(totalLines)} unit="줄" />
            <Metric k="워크스페이스" v={fmt.int(list.length)} unit="개" />
          </div>
          <div className="action-list" style={{ marginTop: 18 }}>
            {stalled.map((w) => (
              <a
                className="action-row action-link"
                href={user.role === 'admin' ? `#/manage/workspaces?workspace=${encodeURIComponent(w.key)}` : `#/collect?workspace=${encodeURIComponent(w.key)}&state=stalled`}
                key={w.key}
              >
                <span><strong>{w.label} 수집 중단</strong><small>마지막 수집 시각과 Slack 연결 상태를 확인합니다.</small></span>
                <Chip tone="bad">조치 필요</Chip>
              </a>
            ))}
            {writeProblems.length > 0 && (
              <a className="action-row action-link" href="#/collect/documents">
                <span><strong>아카이브 저장 실패 {writeProblems.length}개</strong><small>저장 경로 권한과 오류가 발생한 워크스페이스를 확인합니다.</small></span>
                <Chip tone="bad">조치 필요</Chip>
              </a>
            )}
            {brokenDocuments > 0 && (
              <a className="action-row action-link" href="#/collect/documents?state=broken">
                <span><strong>형식 오류 문서 {fmt.int(brokenDocuments)}건</strong><small>검사에 실패한 문서와 실패 사유를 확인합니다.</small></span>
                <Chip tone="watch">확인 필요</Chip>
              </a>
            )}
            {uninvitedChannels > 0 && (
              <a className="action-row action-link" href={user.role === 'admin' ? '#/manage/workspaces' : '#/collect'}>
                <span><strong>미초대 채널 {fmt.int(uninvitedChannels)}개</strong><small>워크스페이스별 Slack 연결과 초대 상태를 확인합니다.</small></span>
                <Chip tone="watch">확인 필요</Chip>
              </a>
            )}
            {!stalled.length && !writeProblems.length && !brokenDocuments && !uninvitedChannels && (
              <div className="action-row">
                <span><strong>조치할 수집 문제가 없습니다</strong><small>모든 워크스페이스가 정상 범위입니다.</small></span>
                <Chip tone="ok">정상</Chip>
              </div>
            )}
          </div>
        </Section>
      )}

      <Section
        title="워크스페이스별 수집 추이"
        note={
          stalled.length ? `수집이 멈춘 워크스페이스 ${stalled.length}개` : '모두 정상 수집 중'
        }
        lead="한 칸이 하루입니다. 오른쪽으로 갈수록 최근이며, 막대는 대화가 수집된 날에만 표시됩니다."
      >
        <Strata items={list} />
      </Section>

    </>
  )
}
