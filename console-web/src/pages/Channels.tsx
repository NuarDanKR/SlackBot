/** 채널 관리 — 담당자를 한 화면에서 정합니다.
 *
 * 설계: docs/design/console-channel-admin.md
 *
 * ## 왜 이 화면이 필요한가
 * 옛날에 만든 채널은 `/채널 수정` 이 안 됩니다. **담당자가 없기 때문입니다.**
 * 담당자가 없으면 검토자도 못 정하고, 그러면 요약 검토 DM도 안 갑니다 —
 * 한 칸이 비어서 그 뒤 기능이 줄줄이 멈춥니다.
 *
 * Slack 에서 채널마다 명령을 치게 하면 수십 개를 하나씩 돌아야 합니다. 그 일은
 * 아무도 하지 않습니다 — 그래서 지금까지 안 된 것입니다.
 *
 * ## 활성도를 같이 보여 주는 이유
 * 담당자를 정할 때 **어느 채널이 살아 있는지** 알아야 합니다. 답변이 0건이고 수집도
 * 멈춘 채널에 담당자를 붙이는 것은 일을 만드는 것입니다. 반대로 답변이 많은데
 * 담당자가 없는 채널이 가장 급합니다 — 그 줄이 맨 위로 옵니다(서버가 정렬).
 *
 * ## 검토자는 보여만 줍니다
 * 검토자는 채널 소유자가 Slack 에서 정합니다. 여기서도 바꾸면 같은 결정이 두 곳에서
 * 나고, 누가 정했는지가 흐려집니다.
 */
import { useEffect, useMemo, useState } from 'react'
import { ApiError, api } from '../api/client'
import { useResource } from '../api/hooks'
import { Empty, Failed, Loading, Metric, PageHead, Section, agoLabel } from '../components/primitives'

interface ChannelRow {
  workspace: string
  workspaceLabel: string
  channelId: string
  channel: string
  owner: string
  ownerSource: string
  managers: string[]
  reviewers: string[]
  sendAt: string
  documents: number
  lines: number
  attachmentLines: number
  lastIngestedAt: string
  answers: number
  lastAnswerAt: string
  needsOwner: boolean
  needsReviewer: boolean
}

interface Candidate {
  workspace: string
  slackUser: string
  name: string
  org: string
}

interface ChannelsPayload {
  summary: {
    channels: number
    missingOwner: number
    missingReviewer: number
    activeMissingOwner: number
  }
  rows: ChannelRow[]
  candidates: Candidate[]
}

interface AssignResult {
  changed: string[]
  skipped: { channel: string; reason: string }[]
  message: string
  ok: boolean
}

interface CollectionJob {
  id: string
  mode: 'files' | 'history'
  status: 'queued' | 'running' | 'completed' | 'failed'
  targetCount: number
  createdAt: string
  startedAt: string | null
  finishedAt: string | null
  exitCode: number | null
  errorCode: string | null
  logTail: string[]
}

interface ReviewChannelResult {
  workspace: string
  channelId: string
  channelName: string
  code: string
  reason: string
  sent: number
  skipped: number
  failed: number
}

interface ReviewResult {
  sent: number
  skipped: number
  failed: number
  generated: number
  canvasFallback: number
  channels: ReviewChannelResult[]
}

interface ReviewJob {
  id: string
  status: 'queued' | 'running' | 'completed' | 'failed'
  workspace: string
  channelId: string
  targets?: { workspace: string; channelId: string }[]
  resend?: boolean
  /** 실제로 무슨 일이 있었는가. 종료 코드는 「보낼 것이 없었다」를 모른다. */
  outcome?: 'sent' | 'partial' | 'nothing-sent' | 'unknown' | null
  result?: ReviewResult | null
  createdAt: string
  startedAt: string | null
  finishedAt: string | null
  exitCode: number | null
  errorCode: string | null
  logTail: string[]
}

type IssueFilter = 'all' | 'missing-owner' | 'missing-reviewer' | 'active-missing-owner'
type ChannelSort = 'priority' | 'channel' | 'answers' | 'last-answer' | 'last-ingested' | 'documents' | 'lines'
type SortDirection = 'asc' | 'desc'

const key = (row: ChannelRow) => `${row.workspace}:${row.channelId}`

/** 사람 이름을 붙여 보여 줍니다. 모르는 ID 는 ID 그대로 — 지어내지 않습니다. */
function personLabel(id: string, people: Map<string, Candidate>): string {
  const found = people.get(id)
  return found ? `${found.name}` : id
}

export function Channels({ onToast }: { onToast: (message: string) => void }) {
  const resource = useResource<ChannelsPayload>('/api/channels')
  const jobResource = useResource<{ job: CollectionJob | null }>('/api/channels/collection-jobs/latest')
  const reviewJobResource = useResource<{ job: ReviewJob | null }>('/api/channels/review-jobs/latest')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [owner, setOwner] = useState('')
  const [ownerMenuOpen, setOwnerMenuOpen] = useState(false)
  const [ownerSearch, setOwnerSearch] = useState('')
  const [overwrite, setOverwrite] = useState(false)
  const [channelSearch, setChannelSearch] = useState('')
  const [workspaceFilter, setWorkspaceFilter] = useState('')
  const [issueFilter, setIssueFilter] = useState<IssueFilter>('all')
  const [sortBy, setSortBy] = useState<ChannelSort>('priority')
  const [sortDirection, setSortDirection] = useState<SortDirection>('desc')
  const [busy, setBusy] = useState(false)
  const [collectionBusy, setCollectionBusy] = useState(false)
  const [reviewBusy, setReviewBusy] = useState(false)
  const [reviewResend, setReviewResend] = useState(false)
  const [result, setResult] = useState<AssignResult | null>(null)
  const job = jobResource.data?.job ?? null
  const jobActive = job?.status === 'queued' || job?.status === 'running'
  const reviewJob = reviewJobResource.data?.job ?? null
  const reviewJobActive = reviewJob?.status === 'queued' || reviewJob?.status === 'running'

  useEffect(() => {
    if (!jobActive) return
    const timer = window.setInterval(jobResource.reload, 3000)
    return () => window.clearInterval(timer)
  }, [jobActive, job?.id, jobResource.reload])

  useEffect(() => {
    if (!reviewJobActive) return
    const timer = window.setInterval(reviewJobResource.reload, 3000)
    return () => window.clearInterval(timer)
  }, [reviewJobActive, reviewJob?.id, reviewJobResource.reload])

  const people = useMemo(() => {
    const map = new Map<string, Candidate>()
    for (const c of resource.data?.candidates ?? []) map.set(c.slackUser, c)
    return map
  }, [resource.data])

  const rows = useMemo(() => {
    const all = resource.data?.rows ?? []
    const originalOrder = new Map(all.map((row, index) => [key(row), index]))
    const search = channelSearch.trim().toLocaleLowerCase('ko-KR')
    const filtered = all.filter((row) => {
      if (workspaceFilter && row.workspace !== workspaceFilter) return false
      if (issueFilter === 'missing-owner' && !row.needsOwner) return false
      if (issueFilter === 'missing-reviewer' && !row.needsReviewer) return false
      if (issueFilter === 'active-missing-owner' && !(row.needsOwner && row.answers > 0)) return false
      if (!search) return true
      const peopleText = [row.owner, ...row.managers, ...row.reviewers]
        .map((id) => personLabel(id, people))
        .join(' ')
      return [row.workspace, row.workspaceLabel, row.channel, peopleText]
        .join(' ')
        .toLocaleLowerCase('ko-KR')
        .includes(search)
    })
    const direction = sortDirection === 'asc' ? 1 : -1
    return [...filtered].sort((a, b) => {
      const workspaceOrder = a.workspaceLabel.localeCompare(b.workspaceLabel, 'ko-KR')
        || a.workspace.localeCompare(b.workspace, 'ko-KR')
      if (workspaceOrder) return workspaceOrder
      if (sortBy === 'priority') {
        return (originalOrder.get(key(a)) ?? 0) - (originalOrder.get(key(b)) ?? 0)
      }
      let compared = 0
      if (sortBy === 'channel') compared = a.channel.localeCompare(b.channel, 'ko-KR')
      if (sortBy === 'answers') compared = a.answers - b.answers
      if (sortBy === 'last-answer') compared = a.lastAnswerAt.localeCompare(b.lastAnswerAt)
      if (sortBy === 'last-ingested') compared = a.lastIngestedAt.localeCompare(b.lastIngestedAt)
      if (sortBy === 'documents') compared = a.documents - b.documents
      if (sortBy === 'lines') compared = a.lines - b.lines
      return compared * direction || a.channel.localeCompare(b.channel, 'ko-KR')
    })
  }, [channelSearch, issueFilter, people, resource.data, sortBy, sortDirection, workspaceFilter])

  const workspaces = useMemo(() => {
    const found = new Map<string, string>()
    for (const row of resource.data?.rows ?? []) found.set(row.workspace, row.workspaceLabel)
    return [...found].map(([workspace, label]) => ({ workspace, label }))
      .sort((a, b) => a.label.localeCompare(b.label, 'ko-KR'))
  }, [resource.data])

  const groupedRows = useMemo(() => {
    const groups: { workspace: string; label: string; rows: ChannelRow[] }[] = []
    for (const row of rows) {
      const last = groups.at(-1)
      if (!last || last.workspace !== row.workspace) {
        groups.push({ workspace: row.workspace, label: row.workspaceLabel, rows: [row] })
      } else {
        last.rows.push(row)
      }
    }
    return groups
  }, [rows])

  const ownerCandidates = useMemo(() => {
    const term = ownerSearch.trim().toLocaleLowerCase('ko-KR')
    const all = resource.data?.candidates ?? []
    if (!term) return all
    return all.filter((candidate) =>
      [candidate.name, candidate.org, candidate.workspace, candidate.slackUser]
        .join(' ')
        .toLocaleLowerCase('ko-KR')
        .includes(term),
    )
  }, [ownerSearch, resource.data])

  const selectedOwner = resource.data?.candidates.find((candidate) => candidate.slackUser === owner)
  /** 선택한 채널 중 검토자가 있는 채널만 실행 대상입니다. */
  const reviewTargets = useMemo(() => {
    const rows = resource.data?.rows ?? []
    return rows.filter((row) => selected.has(key(row)) && row.reviewers.length > 0)
  }, [resource.data, selected])
  const reviewBlocked = useMemo(() => {
    const rows = resource.data?.rows ?? []
    return rows.filter((row) => selected.has(key(row)) && row.reviewers.length === 0)
  }, [resource.data, selected])

  if (resource.loading) return <Loading what="채널 목록" />
  if (resource.error)
    return <Failed what="채널 목록" detail={resource.error.message} onRetry={resource.reload} />
  if (!resource.data) return <Empty title="채널이 없습니다" note="아직 수집된 채널이 없습니다." />

  const totals = resource.data.summary
  // 채널 ID 를 모르는 줄(옛 아카이브 형식)은 고를 수 없습니다. 이름으로 지정하면
  // 다른 채널에 붙을 수 있습니다.
  const selectable = rows.filter((r) => r.channelId)
  const allChecked = selectable.length > 0 && selectable.every((r) => selected.has(key(r)))

  function toggle(row: ChannelRow) {
    if (!row.channelId) return
    const next = new Set(selected)
    const id = key(row)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    setSelected(next)
  }

  function toggleAll() {
    setSelected(allChecked ? new Set() : new Set(selectable.map(key)))
  }

  async function assign() {
    if (!owner || selected.size === 0) return
    setBusy(true)
    setResult(null)
    try {
      const body = { owner, channels: [...selected], overwrite }
      const got = await api.securePost<{ result: AssignResult }>('/api/channels/owner', body)
      setResult(got.result)
      onToast(got.result.message)
      setSelected(new Set())
      resource.reload()
    } catch (e) {
      const error = e as ApiError
      // 왜 안 됐는지 그대로 보여 준다. 「실패했습니다」 만 쓰면 다시 눌러 볼 뿐이다.
      onToast(error.message || '담당자를 지정하지 못했습니다.')
    } finally {
      setBusy(false)
    }
  }

  async function startCollection(mode: 'files' | 'history') {
    if (selected.size === 0 || jobActive) return
    if (
      mode === 'history' &&
      !window.confirm(
        `선택한 ${selected.size}개 채널의 과거 메시지·스레드·파일·현재 Canvas를 소급 수집합니다. 계속할까요?`,
      )
    )
      return
    setCollectionBusy(true)
    try {
      const got = await api.securePost<{ job: CollectionJob }>('/api/channels/collection-jobs', {
        mode,
        channels: [...selected],
      })
      onToast(mode === 'files' ? '채널 파일 수집을 시작했습니다.' : '과거 전체 수집을 시작했습니다.')
      jobResource.reload()
      if (got.job.status === 'failed') onToast('수집 작업을 시작하지 못했습니다.')
    } catch (e) {
      const error = e as ApiError
      onToast(error.message || '수집 작업을 시작하지 못했습니다.')
    } finally {
      setCollectionBusy(false)
    }
  }

  async function startReview() {
    if (reviewTargets.length === 0 || reviewJobActive) return
    const what =
      reviewTargets.length === 1
        ? reviewTargets[0].channel
        : `채널 ${reviewTargets.length}개`
    const again = reviewResend ? '\n오늘 이미 보낸 검토자에게도 다시 보냅니다.' : ''
    if (!window.confirm(`${what}의 예약 시각을 무시하고 오늘 요약 검토를 지금 실행할까요?${again}`))
      return
    setReviewBusy(true)
    try {
      await api.securePost<{ job: ReviewJob }>('/api/channels/review-jobs', {
        channels: reviewTargets.map(key),
        resend: reviewResend,
      })
      onToast(`요약 검토 DM 작업을 시작했습니다 — 대상 ${reviewTargets.length}개 채널.`)
      reviewJobResource.reload()
    } catch (e) {
      const error = e as ApiError
      onToast(error.message || '요약 검토 DM 작업을 시작하지 못했습니다.')
    } finally {
      setReviewBusy(false)
    }
  }

  return (
    <>
      <PageHead
        crumb="수집 · 채널"
        title="채널 관리"
        note="채널 담당자를 정하고, 수집 작업과 요약 검토 DM을 채널별로 실행합니다."
      />

      <Section title="현황">
        <div className="metrics channel-metrics">
          <Metric k="채널" v={String(totals.channels)} unit="개" />
          <Metric k="담당자 없음" v={String(totals.missingOwner)} unit="개" />
          <Metric k="검토자 없음" v={String(totals.missingReviewer)} unit="개" />
          <Metric k="쓰는데 담당자 없음" v={String(totals.activeMissingOwner)} unit="개" />
        </div>
        {totals.activeMissingOwner > 0 && (
          <p className="note">
            답변이 오가는데 담당자가 없는 채널이 {totals.activeMissingOwner}개입니다. 이것부터 정합니다.
          </p>
        )}
      </Section>

      <Section title="담당자 일괄 지정">
        <div className="channel-owner-panel">
          <div className="field channel-owner-field">
            <label className="field-label" htmlFor="channel-owner-picker">담당자</label>
            <div
              className="channel-owner-picker"
              onBlur={(event) => {
                if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
                  setOwnerMenuOpen(false)
                }
              }}
              onKeyDown={(event) => {
                if (event.key === 'Escape') setOwnerMenuOpen(false)
              }}
            >
              <button
                id="channel-owner-picker"
                className="input channel-owner-trigger"
                type="button"
                role="combobox"
                aria-expanded={ownerMenuOpen}
                aria-controls="channel-owner-options"
                onClick={() => setOwnerMenuOpen((open) => !open)}
              >
                <span className={selectedOwner ? '' : 'channel-owner-placeholder'}>
                  {selectedOwner
                    ? `${selectedOwner.name} · ${selectedOwner.org} (${selectedOwner.workspace})`
                    : '담당자를 검색해 선택하세요'}
                </span>
                <span aria-hidden="true">⌄</span>
              </button>
              {ownerMenuOpen && (
                <div className="channel-owner-menu" id="channel-owner-options">
                  <input
                    className="input channel-owner-search"
                    type="search"
                    value={ownerSearch}
                    autoFocus
                    placeholder="이름, 조직 또는 워크스페이스 검색"
                    aria-label="담당자 검색"
                    onChange={(event) => setOwnerSearch(event.target.value)}
                  />
                  <div className="channel-owner-options" role="listbox">
                    {ownerCandidates.map((candidate) => (
                      <button
                        className={`channel-owner-option ${owner === candidate.slackUser ? 'is-selected' : ''}`}
                        type="button"
                        role="option"
                        aria-selected={owner === candidate.slackUser}
                        key={`${candidate.workspace}:${candidate.slackUser}`}
                        onClick={() => {
                          setOwner(candidate.slackUser)
                          setOwnerSearch('')
                          setOwnerMenuOpen(false)
                        }}
                      >
                        <strong>{candidate.name}</strong>
                        <span>{candidate.org || '조직 미등록'} · {candidate.workspace}</span>
                      </button>
                    ))}
                    {ownerCandidates.length === 0 && (
                      <div className="channel-owner-empty">검색 결과가 없습니다.</div>
                    )}
                  </div>
                </div>
              )}
            </div>
          </div>
          <div className="channel-owner-settings">
            <label className="checkbox">
            <input
              type="checkbox"
              checked={overwrite}
              onChange={(e) => setOverwrite(e.target.checked)}
            />
            이미 담당자가 있어도 바꾸기
            </label>
            <span className="channel-selection-count">현재 {selected.size}개 채널 선택</span>
          </div>
          <button className="btn btn-primary channel-owner-submit" type="button" disabled={busy || !owner || selected.size === 0} onClick={assign}>
            {busy ? '지정 중…' : `${selected.size}개 채널에 지정`}
          </button>
        </div>
        {resource.data.candidates.length === 0 && (
          <p className="note warn">
            담당자로 고를 사람이 없습니다. Slack 계정과 사번이 이어진 재직자만 지정할 수
            있습니다 — <code>scripts/backfill_identity.py</code> 로 매핑을 먼저 만듭니다.
          </p>
        )}
        {overwrite && (
          <p className="note warn">
            기존 담당자는 그 채널을 만든 실제 요청자일 수 있습니다. 바꾸면 그 사람은 권한을 잃습니다.
          </p>
        )}
        {result && !result.ok && (
          <div className="note warn">
            <strong>{result.message}</strong>
            <ul>
              {result.skipped.map((s) => (
                <li key={s.channel}>
                  {s.channel} — {s.reason}
                </li>
              ))}
            </ul>
          </div>
        )}
      </Section>

      <Section
        title="수집 작업"
        lead="선택한 채널의 파일 탭만 동기화하거나, 봇 초대 이전 메시지와 스레드까지 소급 수집합니다. 작업은 한 번에 하나만 실행됩니다."
      >
        <div className="toolbar">
          <button
            type="button"
            className="btn"
            disabled={collectionBusy || jobActive || selected.size === 0}
            onClick={() => startCollection('files')}
          >
            {selected.size}개 채널 파일 수집
          </button>
          <button
            type="button"
            className="btn btn-primary"
            disabled={collectionBusy || jobActive || selected.size === 0}
            onClick={() => startCollection('history')}
          >
            {selected.size}개 채널 과거 전체 수집
          </button>
          {jobActive && <span className="chip info">수집 실행 중</span>}
        </div>
        {job && (
          <div className={`notice ${job.status === 'failed' ? 'bad' : ''}`}>
            <div className="notice-kind">
              {job.mode === 'files' ? '파일 수집' : '과거 전체 수집'}
            </div>
            <div>
              <div className="notice-title">
                {job.status === 'queued' && '실행 대기'}
                {job.status === 'running' && `실행 중 · 대상 ${job.targetCount}개 채널`}
                {job.status === 'completed' && `완료 · 대상 ${job.targetCount}개 채널`}
                {job.status === 'failed' && `실패 · ${job.errorCode ?? '원인 미확인'}`}
              </div>
              {job.logTail.length > 0 && (
                <pre className="channel-job-log">{job.logTail.join('\n')}</pre>
              )}
            </div>
          </div>
        )}
        {jobResource.error && <p className="note warn">작업 상태를 읽지 못했습니다: {jobResource.error.message}</p>}
      </Section>

      <Section
        title="요약 검토 DM"
        lead="선택한 채널의 예약 시각과 당일 생성 잠금을 우회해, 마지막 처리 이후 새 원문으로 검토 Canvas와 DM 생성을 실행합니다. 새 원문이나 검토 후보가 없으면 DM은 보내지 않고 사유를 표시합니다."
      >
        <div className="toolbar">
          <button
            type="button"
            className="btn btn-primary"
            disabled={reviewBusy || reviewJobActive || reviewTargets.length === 0}
            onClick={startReview}
          >
            {reviewBusy || reviewJobActive
              ? '검토 DM 실행 중'
              : `선택 채널 ${reviewTargets.length}개 검토 DM 지금 발송`}
          </button>
          <label className="checkbox">
            <input
              type="checkbox"
              checked={reviewResend}
              onChange={(event) => setReviewResend(event.target.checked)}
            />
            오늘 이미 보낸 검토자에게도 다시 보내기
          </label>
          {reviewBlocked.length > 0 && (
            <span className="note warn">
              활성 검토자가 없어 제외한 채널 {reviewBlocked.length}개:{' '}
              {reviewBlocked.map((row) => row.channel).join(', ')}
            </span>
          )}
          {selected.size === 0 && <span className="note">채널을 하나 이상 선택하세요.</span>}
        </div>
        {reviewJob && (
          <div
            className={`notice ${
              reviewJob.status === 'failed' || reviewJob.outcome === 'nothing-sent' ? 'bad' : ''
            }`}
          >
            <div className="notice-kind">요약 검토 DM</div>
            <div>
              <div className="notice-title">
                {reviewJob.status === 'queued' && '실행 대기'}
                {reviewJob.status === 'running' && '요약 및 Canvas 생성 중'}
                {reviewJob.status === 'failed' && `실패 · ${reviewJob.errorCode ?? '원인 미확인'}`}
                {reviewJob.status === 'completed' &&
                  (reviewJob.outcome === 'sent'
                    ? `발송 완료 · DM ${reviewJob.result?.sent ?? 0}건`
                    : reviewJob.outcome === 'partial'
                      ? `일부 발송 · DM ${reviewJob.result?.sent ?? 0}건 · 실패 ${reviewJob.result?.failed ?? 0}건`
                      : 'DM이 한 건도 가지 않았습니다')}
              </div>
              {reviewJob.status === 'completed' && reviewJob.outcome === 'nothing-sent' && (
                <p className="note warn">
                  실행 자체는 끝났지만 아무도 받지 못했습니다. 채널별 사유를 보고 조치하세요.
                </p>
              )}
              <div className="notice-detail mono">
                {(reviewJob.targets ?? [{ workspace: reviewJob.workspace, channelId: reviewJob.channelId }])
                  .map((target) => `${target.workspace}:${target.channelId}`)
                  .join(', ')}
                {reviewJob.resend ? ' · 재발송' : ''}
              </div>
              {reviewJob.result && reviewJob.result.channels.length > 0 && (
                <ul className="review-outcomes">
                  {reviewJob.result.channels.map((row) => (
                    <li key={`${row.workspace}:${row.channelId}`}>
                      <span className="mono">{row.channelName || row.channelId}</span> —{' '}
                      {row.sent > 0 ? `DM ${row.sent}건 발송` : row.reason}
                    </li>
                  ))}
                </ul>
              )}
              {reviewJob.logTail.length > 0 && (
                <pre className="channel-job-log">{reviewJob.logTail.join('\n')}</pre>
              )}
            </div>
          </div>
        )}
        {reviewJobResource.error && (
          <p className="note warn">작업 상태를 읽지 못했습니다: {reviewJobResource.error.message}</p>
        )}
      </Section>

      <Section
        title={`채널 ${rows.length}개`}
        note={`전체 ${totals.channels}개 중 표시`}
      >
        <div className="channel-table-tools">
          <div className="field channel-table-search">
            <label className="field-label" htmlFor="channel-search">채널 또는 담당자 검색</label>
            <input id="channel-search" className="input" type="search" value={channelSearch} placeholder="채널명, 담당자, 검토자" onChange={(event) => { setChannelSearch(event.target.value); setSelected(new Set()) }} />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="channel-workspace-filter">워크스페이스</label>
            <select id="channel-workspace-filter" className="input" value={workspaceFilter} onChange={(event) => { setWorkspaceFilter(event.target.value); setSelected(new Set()) }}>
              <option value="">전체 워크스페이스</option>
              {workspaces.map((workspace) => <option key={workspace.workspace} value={workspace.workspace}>{workspace.label} ({workspace.workspace})</option>)}
            </select>
          </div>
          <div className="field">
            <label className="field-label" htmlFor="channel-issue-filter">관리 상태</label>
            <select id="channel-issue-filter" className="input" value={issueFilter} onChange={(event) => { setIssueFilter(event.target.value as IssueFilter); setSelected(new Set()) }}>
              <option value="all">모든 채널</option>
              <option value="missing-owner">담당자 없음</option>
              <option value="missing-reviewer">검토자 없음</option>
              <option value="active-missing-owner">사용 중·담당자 없음</option>
            </select>
          </div>
          <div className="field">
            <label className="field-label" htmlFor="channel-sort">정렬</label>
            <div className="channel-sort-control">
              <select id="channel-sort" className="input" value={sortBy} onChange={(event) => { const next = event.target.value as ChannelSort; setSortBy(next); setSortDirection(next === 'channel' ? 'asc' : 'desc') }}>
                <option value="priority">관리 우선순위</option>
                <option value="channel">채널명</option>
                <option value="answers">답변 수</option>
                <option value="last-answer">마지막 답변</option>
                <option value="last-ingested">마지막 수집</option>
                <option value="documents">문서 수</option>
                <option value="lines">원문 줄 수</option>
              </select>
              <button className="btn channel-sort-direction" type="button" disabled={sortBy === 'priority'} aria-label={sortDirection === 'desc' ? '현재 내림차순, 오름차순으로 변경' : '현재 오름차순, 내림차순으로 변경'} onClick={() => setSortDirection((direction) => direction === 'desc' ? 'asc' : 'desc')}>
                {sortBy === 'priority' ? '우선순위 고정' : sortDirection === 'desc' ? '내림차순 ↓' : '오름차순 ↑'}
              </button>
            </div>
          </div>
        </div>
        {rows.length === 0 ? (
          <Empty
            title="해당하는 채널이 없습니다"
            note="검색어 또는 필터를 바꾸어 다시 확인해 주세요."
          />
        ) : (
          <div className="grid-scroll">
            <table className="grid">
              <thead>
                <tr>
                  <th>
                    <input type="checkbox" checked={allChecked} onChange={toggleAll} />
                  </th>
                  <th>채널</th>
                  <th>담당자</th>
                  <th>검토자</th>
                  <th className="num">문서</th>
                  <th className="num">원문 줄</th>
                  <th className="num">첨부 줄</th>
                  <th>마지막 수집</th>
                  <th className="num">답변</th>
                  <th>마지막 답변</th>
                </tr>
              </thead>
              {groupedRows.map((group) => (
                <tbody key={group.workspace}>
                  <tr className="channel-workspace-row">
                    <th colSpan={10} scope="rowgroup">
                      <span>{group.label}</span>
                      <span className="channel-workspace-key">{group.workspace}</span>
                      <span className="channel-workspace-count">{group.rows.length}개 채널</span>
                    </th>
                  </tr>
                  {group.rows.map((row) => (
                  <tr key={key(row)} className={row.needsOwner ? 'row-warn' : undefined}>
                    <td>
                      <input
                        type="checkbox"
                        disabled={!row.channelId}
                        checked={selected.has(key(row))}
                        onChange={() => toggle(row)}
                        title={row.channelId ? '' : '채널 ID 를 모릅니다(옛 아카이브 형식)'}
                      />
                    </td>
                    <td className="wrap">{row.channel}</td>
                    <td>
                      {row.owner ? (
                        personLabel(row.owner, people)
                      ) : (
                        <span className="warn">없음</span>
                      )}
                      {row.managers.length > 0 && (
                        <span className="dim"> +{row.managers.length}</span>
                      )}
                    </td>
                    <td>
                      {row.reviewers.length === 0 ? (
                        <span className="warn">없음</span>
                      ) : (
                        <>
                          {row.reviewers.map((r) => personLabel(r, people)).join(', ')}
                          {row.sendAt && <span className="dim"> · {row.sendAt}</span>}
                        </>
                      )}
                    </td>
                    <td className="num">{row.documents}</td>
                    <td className="num">{row.lines}</td>
                    <td className="num">{row.attachmentLines}</td>
                    <td>{row.lastIngestedAt ? agoLabel(row.lastIngestedAt) : '—'}</td>
                    <td className="num">{row.answers}</td>
                    <td>{row.lastAnswerAt ? agoLabel(row.lastAnswerAt) : '—'}</td>
                  </tr>
                  ))}
                </tbody>
              ))}
            </table>
          </div>
        )}
        <p className="note">
          검토자는 이 화면에서 바꾸지 않습니다. 채널 소유자가 Slack 에서 <code>/채널 수정</code> 으로
          정합니다 — 같은 결정이 두 곳에서 나면 누가 정했는지가 흐려집니다.
        </p>
      </Section>
    </>
  )
}
