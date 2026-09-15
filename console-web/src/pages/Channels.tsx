/** 채널 관리 — 담당자를 한 화면에서 정합니다.
 *
 * 설계: docs/design/console-channel-admin.md
 *
 * ## 왜 이 화면이 필요한가
 * 옛날에 만든 채널은 `/채널 수정` 이 안 됩니다. **담당자가 없기 때문입니다.**
 * 담당자가 없으면 검토자도 못 정하고, 그러면 첨부 검수 DM 도 안 갑니다 —
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

const key = (row: ChannelRow) => `${row.workspace}:${row.channelId}`

/** 사람 이름을 붙여 보여 줍니다. 모르는 ID 는 ID 그대로 — 지어내지 않습니다. */
function personLabel(id: string, people: Map<string, Candidate>): string {
  const found = people.get(id)
  return found ? `${found.name}` : id
}

export function Channels({ onToast }: { onToast: (message: string) => void }) {
  const resource = useResource<ChannelsPayload>('/api/channels')
  const jobResource = useResource<{ job: CollectionJob | null }>('/api/channels/collection-jobs/latest')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [owner, setOwner] = useState('')
  const [overwrite, setOverwrite] = useState(false)
  const [filter, setFilter] = useState<'missing' | 'all'>('all')
  const [busy, setBusy] = useState(false)
  const [collectionBusy, setCollectionBusy] = useState(false)
  const [result, setResult] = useState<AssignResult | null>(null)
  const job = jobResource.data?.job ?? null
  const jobActive = job?.status === 'queued' || job?.status === 'running'

  useEffect(() => {
    if (!jobActive) return
    const timer = window.setInterval(jobResource.reload, 3000)
    return () => window.clearInterval(timer)
  }, [jobActive, job?.id, jobResource.reload])

  const people = useMemo(() => {
    const map = new Map<string, Candidate>()
    for (const c of resource.data?.candidates ?? []) map.set(c.slackUser, c)
    return map
  }, [resource.data])

  const rows = useMemo(() => {
    const all = resource.data?.rows ?? []
    return filter === 'missing' ? all.filter((r) => r.needsOwner) : all
  }, [resource.data, filter])

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

  return (
    <>
      <PageHead
        crumb="수집 · 채널"
        title="채널 관리"
        note="채널 담당자를 정하고, 선택한 채널의 파일 동기화와 봇 초대 이전 데이터 소급 수집을 실행합니다."
      />

      <Section title="현황">
        <div className="metrics">
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
        <div className="toolbar">
          <label>
            담당자
            <select value={owner} onChange={(e) => setOwner(e.target.value)}>
              <option value="">— 고르세요 —</option>
              {resource.data.candidates.map((c) => (
                <option key={`${c.workspace}:${c.slackUser}`} value={c.slackUser}>
                  {c.name} · {c.org} ({c.workspace})
                </option>
              ))}
            </select>
          </label>
          <label>
            <input
              type="checkbox"
              checked={overwrite}
              onChange={(e) => setOverwrite(e.target.checked)}
            />
            이미 담당자가 있어도 바꾸기
          </label>
          <button type="button" disabled={busy || !owner || selected.size === 0} onClick={assign}>
            {busy ? '지정 중…' : `${selected.size}개 채널에 지정`}
          </button>
          <label>
            <input
              type="checkbox"
              checked={filter === 'missing'}
              onChange={(e) => {
                setFilter(e.target.checked ? 'missing' : 'all')
                setSelected(new Set())
              }}
            />
            담당자 없는 채널만
          </label>
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

      <Section title={`채널 ${rows.length}개`}>
        {rows.length === 0 ? (
          <Empty
            title="해당하는 채널이 없습니다"
            note={
              filter === 'missing'
                ? '담당자가 모두 지정되어 있습니다.'
                : '봇이 참여한 수집 대상 채널이 없습니다.'
            }
          />
        ) : (
          <div className="grid-scroll">
            <table className="grid">
              <thead>
                <tr>
                  <th>
                    <input type="checkbox" checked={allChecked} onChange={toggleAll} />
                  </th>
                  <th>워크스페이스</th>
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
              <tbody>
                {rows.map((row) => (
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
                    <td>{row.workspaceLabel}</td>
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
