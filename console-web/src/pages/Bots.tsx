import type { ReactNode } from 'react'
import { useMemo, useState } from 'react'
import { ApiError, api } from '../api/client'
import { useResource } from '../api/hooks'
import { Empty, Failed, Loading, PageHead, Section } from '../components/primitives'
import {
  CONNECTION_STATES,
  ROUTE_MODES,
  meaningOf,
  tooltipOf,
} from '../botModes'
import { BotAudit, BotManifests, BotRouting } from './BotsRouting'
import type { ConsoleUser } from '../types'
import './Bots.css'

/**
 * 봇 관리 — **하나의 봇, 여러 연결.**
 *
 * 서버: `src/tybot/console/bot_admin.py` · 설계:
 * `docs/design/workspace-service-console-redesign.md`
 *
 * Hermes 는 한 줄이다. PF 가 Slack 으로 직접 부르는 것과 Master 가 내부로 부르는
 * 것은 **같은 봇의 다른 연결**이다. 두 줄로 나누면 하나를 끈 사람이 다른 하나도
 * 끈 줄 안다.
 *
 * ## 지금은 기록이지 적용이 아니다
 *
 * 돌고 있는 프로세스는 아직 옛 표를 읽는다(`runtimeEffect.appliesNow === false`).
 * 그래서 이 화면은 저장을 **「반영됐다」 로 말하지 않는다.** 저장 성공을 초록불로
 * 보여 주면, 연결을 끈 사람은 수집이 멈춘 줄 알고 자리를 뜬다. 그 오해는 조용하고
 * 오래간다 — 아무 오류도 안 나기 때문이다.
 */

export type Binding = {
  workspace: string
  type: 'slack_socket' | 'master_internal'
  state?: string
  mode?: string
  assigned?: boolean
  teamId?: string
  botUserId?: string
  identityOk?: boolean | null
  identityError?: string
  identityCheckedAt?: string | null
  manifestId?: string
  manifestAttestedSha256?: string
  manifestAttestedAt?: string | null
  manifestAttestedBy?: string
  botTokenMask?: string
  appTokenMask?: string
  tokenCount?: number
  note?: string
  updatedAt?: string | null
  updatedBy?: string
}

export type Runtime = {
  state: string
  health: string
  version: string
  contractVersion: string
  errorCode: string
  domain: string
  adapter: string
}

export type Bot = {
  key: string
  displayName: string
  category: 'orchestrator' | 'collector' | 'specialist'
  ownerTeam: string
  slackConnectable: boolean
  internallyInvokable: boolean
  state: string
  runtime: Runtime | null
  bindings: Binding[]
}

export type RuntimeEffect = {
  appliesNow: boolean
  summary: string
  details: string[]
}

type BotsResponse = { bots: Bot[]; runtimeEffect: RuntimeEffect }

type WorkspaceBot = {
  key: string
  displayName: string
  category: string
  slackConnectable: boolean
  internallyInvokable: boolean
  runtime: Runtime | null
  slack: Binding[]
  internal: Binding[]
}

type WorkspaceResponse = {
  workspace: string
  bots: WorkspaceBot[]
  runtimeEffect: RuntimeEffect
}

const CATEGORY_LABEL: Record<string, string> = {
  orchestrator: '진입·전달',
  collector: '수집',
  specialist: '전문',
}

/**
 * 저장이 런타임에 닿지 않는다는 사실을 **화면 위쪽에** 둔다.
 *
 * 각 버튼 옆에 작게 적으면 사람은 버튼만 본다. 그리고 「성공」 이라는 단어를 쓰지
 * 않는다 — 기록은 됐지만 아무것도 시작되지 않았기 때문이다.
 */
export function RuntimeNotice({ effect }: { effect?: RuntimeEffect }) {
  if (!effect || effect.appliesNow) return null
  return (
    <div className="bots-notice" role="note">
      <div className="bots-notice-title">{effect.summary}</div>
      <ul className="bots-notice-list">
        {effect.details.map((line) => (
          <li key={line}>{line.replace(/\*\*/g, '')}</li>
        ))}
      </ul>
    </div>
  )
}

type ManifestDetail = {
  manifestId: string
  botKey: string
  sourcePath: string
  sha256: string
  content: string
}

/**
 * 연결 상세에서 바로 보는 Slack App Manifest.
 *
 * 별도 탭을 찾아가야만 설치 파일을 볼 수 있으면, 사람은 화면 밖에서 파일을 구해
 * 온다 — 그 순간 무엇을 붙여 넣었는지 아무도 모른다.
 *
 * **보기는 대조가 아니다.** 여기서 여닫고 복사해도 「확인함」 이 되지 않는다.
 * 적용 확인은 사람이 Slack 설정과 SHA-256 을 대조하고 사유를 남기는 별도 동작이고,
 * 그건 Manifest 탭에 있다(§4.2).
 */
function ManifestBlock({
  botKey,
  onToast,
}: {
  botKey: string
  onToast: (message: string) => void
}) {
  const [open, setOpen] = useState(false)
  const [detail, setDetail] = useState<ManifestDetail | null>(null)
  const [problem, setProblem] = useState('')

  async function load() {
    if (detail || problem) {
      setOpen(!open)
      return
    }
    try {
      const found = await api.get<ManifestDetail>(
        `/api/bot-manifests/${botKey}/slack_socket`,
      )
      setDetail(found)
      setOpen(true)
    } catch (error) {
      // 「정본 미등록」 과 「불러오지 못함」 은 사람이 할 일이 다르다.
      setProblem(
        error instanceof ApiError && error.status === 404
          ? '정본 미등록'
          : '정본을 불러오지 못했습니다.',
      )
      setOpen(true)
    }
  }

  async function copy() {
    if (!detail) return
    try {
      await navigator.clipboard.writeText(detail.content)
      onToast('Manifest 를 복사했습니다. 복사만으로는 대조 기록이 남지 않습니다.')
    } catch {
      onToast('복사하지 못했습니다. 아래 내용을 직접 선택해 복사하세요.')
    }
  }

  return (
    <div className="bots-manifest">
      <div className="bots-actions">
        <button type="button" className="btn ghost btn-sm" onClick={load}>
          {open ? 'Manifest 접기' : 'Slack App Manifest 보기'}
        </button>
        {detail ? (
          <button type="button" className="btn ghost btn-sm" onClick={copy}>
            복사
          </button>
        ) : null}
      </div>
      {open && problem ? (
        <p className="bots-note">
          {problem === '정본 미등록'
            ? '정본 미등록 — 이 봇의 Slack App Manifest 는 아직 저장소에 없습니다. 임의로 만들지 마세요.'
            : problem}
        </p>
      ) : null}
      {open && detail ? (
        <>
          <p className="bots-note">
            {detail.sourcePath} · SHA-256 {detail.sha256}
          </p>
          <p className="bots-note">
            보기·복사는 설치 편의 기능입니다. 대조 기록은 Manifest 탭에서 사유와 함께
            남깁니다.
          </p>
          <pre className="bots-manifest-body">{detail.content}</pre>
        </>
      ) : null}
    </div>
  )
}

function StateChip({
  table,
  value,
}: {
  table: typeof CONNECTION_STATES | typeof ROUTE_MODES
  value: string | undefined
}) {
  const meaning = meaningOf(table, value)
  // 내부 키는 title 에만 둔다. 주 화면에 나란히 붙이면 사람이 키를 읽고 뜻은
  // 장식이 된다.
  return (
    <span className={`bots-chip tone-${meaning.tone}`} title={tooltipOf(meaning)}>
      {meaning.label}
    </span>
  )
}

function BindingCell({ binding }: { binding: Binding }) {
  if (binding.type === 'master_internal') {
    return (
      <span className="bots-binding">
        <span className="bots-binding-kind">Master 호출</span>
        <StateChip table={ROUTE_MODES} value={binding.mode} />
        {binding.assigned ? null : <span className="bots-warn">배정 없음</span>}
      </span>
    )
  }
  return (
    <span className="bots-binding">
      <span className="bots-binding-kind">Slack 직접</span>
      <StateChip table={CONNECTION_STATES} value={binding.state} />
    </span>
  )
}

/** 런타임은 **정체성과 다른 칸**이다. 합치면 한쪽을 끈 것이 다른 쪽까지 끈 것으로 읽힌다. */
function RuntimeCell({ runtime }: { runtime: Runtime | null }) {
  if (!runtime) return <span className="bots-muted">해당 없음</span>
  const tone = runtime.health === 'ok' && runtime.state === 'enabled' ? 'good' : 'warn'
  return (
    <span className={`bots-chip tone-${tone}`} title={`contract ${runtime.contractVersion}`}>
      {runtime.state === 'enabled' ? '런타임 사용' : '런타임 중지'}
      {runtime.health === 'ok' ? '' : ' · 점검 필요'}
      {runtime.version ? ` · ${runtime.version}` : ''}
    </span>
  )
}

export function BotList({
  navigate,
  onSelect,
}: {
  navigate: (path: string) => void
  onSelect: (key: string) => void
}) {
  const resource = useResource<BotsResponse>('/api/bots')
  if (resource.loading && !resource.data) return <Loading what="봇 목록" />
  if (resource.error || !resource.data) {
    return <Failed what="봇 목록을" detail={resource.error?.message ?? ""} onRetry={resource.reload} />
  }
  const { bots, runtimeEffect } = resource.data
  const workspaces = Array.from(
    new Set(bots.flatMap((bot) => bot.bindings.map((b) => b.workspace))),
  ).sort()

  return (
    <>
      <RuntimeNotice effect={runtimeEffect} />
      <Section
        title="봇"
        lead="봇 하나에 연결이 여럿입니다. Slack 직접 연결과 Master 내부 호출은 같은 봇의 서로 다른 연결 방식입니다."
      >
        {bots.length === 0 ? (
          <Empty title="등록된 봇이 없습니다" note="스키마 적용 후 카탈로그가 채워집니다." />
        ) : (
          <div className="table-wrap">
            <table className="table bots-table">
              <thead>
                <tr>
                  <th>봇</th>
                  <th>구분</th>
                  <th>런타임</th>
                  <th>연결</th>
                </tr>
              </thead>
              <tbody>
                {bots.map((bot) => (
                  <tr key={bot.key}>
                    <th scope="row">
                      <button
                        type="button"
                        className="linkish"
                        onClick={() => onSelect(bot.key)}
                      >
                        {bot.displayName}
                      </button>
                      <div className="bots-sub">{bot.ownerTeam || '담당 미지정'}</div>
                    </th>
                    <td>{CATEGORY_LABEL[bot.category] ?? bot.category}</td>
                    <td><RuntimeCell runtime={bot.runtime} /></td>
                    <td>
                      {bot.bindings.length === 0 ? (
                        <span className="bots-muted">연결 없음</span>
                      ) : (
                        <div className="bots-bindings">
                          {bot.bindings.map((binding) => (
                            <div key={`${binding.workspace}-${binding.type}`}>
                              <span className="bots-ws">{binding.workspace}</span>
                              <BindingCell binding={binding} />
                            </div>
                          ))}
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      <Section
        title="워크스페이스"
        lead="워크스페이스별로 어떤 봇이 붙어 있는지 봅니다. 연결을 바꾸려면 워크스페이스를 고르세요."
      >
        {workspaces.length === 0 ? (
          <Empty
            title="연결된 워크스페이스가 없습니다"
            note="워크스페이스를 먼저 등록한 뒤 이 화면에서 봇을 붙입니다."
          />
        ) : (
          <div className="bots-ws-list">
            {workspaces.map((workspace) => (
              <button
                key={workspace}
                type="button"
                className="bots-ws-card"
                onClick={() => navigate(`/manage/bots/connections?workspace=${workspace}`)}
              >
                <span className="bots-ws-key">{workspace}</span>
                <span className="bots-ws-note">
                  {bots.filter((bot) =>
                    bot.bindings.some((b) => b.workspace === workspace),
                  ).length}
                  개 봇
                </span>
              </button>
            ))}
          </div>
        )}
      </Section>
    </>
  )
}

export function WorkspaceConnections({
  workspace,
  onToast,
  onWorkspaceChange,
}: {
  workspace: string
  onToast: (message: string) => void
  onWorkspaceChange: (workspace: string) => void
}) {
  const bots = useResource<BotsResponse>('/api/bots')
  const [data, setData] = useState<WorkspaceResponse | null>(null)
  const [busy, setBusy] = useState('')
  const [draft, setDraft] = useState({ bot: '', botToken: '', appToken: '', reason: '' })
  const resource = useResource<WorkspaceResponse>(
    workspace ? `/api/workspaces/${encodeURIComponent(workspace)}/bot-connections` : null,
    [workspace],
  )
  const view = data && data.workspace === workspace ? data : resource.data
  const workspaces = useMemo(() => {
    const rows = bots.data?.bots ?? []
    return Array.from(new Set(rows.flatMap((bot) => bot.bindings.map((b) => b.workspace)))).sort()
  }, [bots.data])

  async function save(botKey: string) {
    if (!draft.reason.trim()) {
      onToast('변경 사유를 적어 주세요.')
      return
    }
    setBusy(botKey)
    try {
      const next = await api.put<WorkspaceResponse>(
        `/api/workspaces/${encodeURIComponent(workspace)}/bot-connections/${botKey}/slack`,
        { botToken: draft.botToken, appToken: draft.appToken, reason: draft.reason },
      )
      setData(next)
      onToast('토큰 등록과 Slack 연결 확인을 마쳤습니다. 수집은 Archiver 실행 후 시작됩니다.')
      // 입력란은 **즉시 비운다.** 화면에 평문이 남아 있을 이유가 없다.
      setDraft({ bot: '', botToken: '', appToken: '', reason: '' })
    } catch (error) {
      onToast(error instanceof ApiError ? error.message : '저장하지 못했습니다.')
    } finally {
      // 서버가 토큰을 저장한 뒤 Slack 확인만 실패할 수도 있다. 성공 여부와 무관하게
      // 평문은 입력란에서 지우고, 재시도할 때 다시 입력받는다.
      setDraft((current) => current.bot === botKey
        ? { ...current, botToken: '', appToken: '' }
        : current)
      setBusy('')
    }
  }

  async function changeState(botKey: string, state: 'disabled' | 'retired', reason: string) {
    setBusy(botKey)
    try {
      const next = await api.securePost<WorkspaceResponse>(
        `/api/workspaces/${encodeURIComponent(workspace)}/bot-connections/${botKey}/slack/state`,
        { state, reason: reason || '연결 정리' },
      )
      setData(next)
      onToast('연결 상태를 기록했습니다. 돌고 있는 프로세스는 그대로입니다.')
    } catch (error) {
      onToast(error instanceof ApiError ? error.message : '바꾸지 못했습니다.')
    } finally {
      setBusy('')
    }
  }

  if (!workspace) {
    return (
      <Section title="워크스페이스를 고르세요" lead="연결은 워크스페이스마다 다릅니다.">
        <div className="bots-ws-list">
          {workspaces.map((key) => (
            <button
              key={key}
              type="button"
              className="bots-ws-card"
              onClick={() => onWorkspaceChange(key)}
            >
              <span className="bots-ws-key">{key}</span>
            </button>
          ))}
        </div>
      </Section>
    )
  }
  if (resource.loading && !view) return <Loading what="워크스페이스 연결" />
  if (resource.error && !view) {
    return <Failed what="워크스페이스 연결을" detail={resource.error?.message ?? ""} onRetry={resource.reload} />
  }
  if (!view) return null

  return (
    <>
      <RuntimeNotice effect={view.runtimeEffect} />
      <Section
        title={`${workspace} 연결`}
        lead="Slack 직접 연결에는 봇/앱 토큰 쌍이 필요합니다. Slack 앱과 토큰은 워크스페이스마다 별도입니다 — 다른 워크스페이스의 토큰을 여기 넣지 마세요. Master 내부 호출에는 토큰이 없습니다."
        aside={
          <select
            className="input"
            value={workspace}
            onChange={(event) => onWorkspaceChange(event.target.value)}
            aria-label="워크스페이스"
          >
            {workspaces.map((key) => (
              <option key={key} value={key}>{key}</option>
            ))}
          </select>
        }
      >
        <div className="bots-cards">
          {view.bots.map((bot) => {
            const slack = bot.slack[0]
            const internal = bot.internal[0]
            const editing = draft.bot === bot.key
            return (
              <article className="bots-card" key={bot.key}>
                <header className="bots-card-head">
                  <div>
                    <h3>{bot.displayName}</h3>
                    <div className="bots-sub">
                      {CATEGORY_LABEL[bot.category] ?? bot.category}
                    </div>
                  </div>
                  <RuntimeCell runtime={bot.runtime} />
                </header>

                {bot.slackConnectable ? (
                  <div className="bots-row">
                    <div className="bots-row-head">
                      <span className="bots-binding-kind">Slack 직접 연결</span>
                      <StateChip table={CONNECTION_STATES} value={slack?.state ?? 'draft'} />
                    </div>
                    <dl className="bots-facts">
                      <div>
                        <dt>봇 토큰</dt>
                        <dd>{slack?.botTokenMask || '없음'}</dd>
                      </div>
                      <div>
                        <dt>앱 토큰</dt>
                        <dd>{slack?.appTokenMask || '없음'}</dd>
                      </div>
                      <div>
                        <dt>Slack 연결</dt>
                        <dd>
                          {slack?.identityOk
                            ? `확인됨 · ${slack.teamId} · ${slack.botUserId}`
                            : slack?.identityError || '토큰 미등록'}
                        </dd>
                      </div>
                      <div>
                        <dt>Manifest 대조</dt>
                        <dd>
                          {slack?.manifestAttestedSha256
                            ? `${slack.manifestId} · ${slack.manifestAttestedBy}`
                            : '아직 대조하지 않음'}
                        </dd>
                      </div>
                    </dl>

                    <ManifestBlock botKey={bot.key} onToast={onToast} />

                    {editing ? (
                      <div className="bots-form">
                        <label>
                          봇 토큰
                          <input
                            className="input"
                            type="password"
                            autoComplete="off"
                            placeholder="xoxb-"
                            value={draft.botToken}
                            onChange={(event) =>
                              setDraft({ ...draft, botToken: event.target.value })
                            }
                          />
                        </label>
                        <label>
                          앱 토큰
                          <input
                            className="input"
                            type="password"
                            autoComplete="off"
                            placeholder="xapp-"
                            value={draft.appToken}
                            onChange={(event) =>
                              setDraft({ ...draft, appToken: event.target.value })
                            }
                          />
                        </label>
                        <label>
                          변경 사유
                          <input
                            className="input"
                            value={draft.reason}
                            onChange={(event) =>
                              setDraft({ ...draft, reason: event.target.value })
                            }
                          />
                        </label>
                        <div className="bots-actions">
                          <button
                            type="button"
                            className="btn"
                            disabled={busy === bot.key}
                            onClick={() => save(bot.key)}
                          >
                            토큰 저장
                          </button>
                          <button
                            type="button"
                            className="btn ghost"
                            onClick={() =>
                              setDraft({ bot: '', botToken: '', appToken: '', reason: '' })
                            }
                          >
                            취소
                          </button>
                        </div>
                      </div>
                    ) : (
                      <div className="bots-actions">
                        <button
                          type="button"
                          className="btn ghost"
                          onClick={() =>
                            setDraft({ bot: bot.key, botToken: '', appToken: '', reason: '' })
                          }
                        >
                          {slack?.tokenCount ? '토큰 교체' : '토큰 등록'}
                        </button>
                        <button
                          type="button"
                          className="btn ghost"
                          disabled={!slack || busy === bot.key}
                          onClick={() => changeState(bot.key, 'disabled', '연결 중지')}
                        >
                          연결 중지
                        </button>
                      </div>
                    )}
                  </div>
                ) : null}

                {internal ? (
                  <div className="bots-row">
                    <div className="bots-row-head">
                      <span className="bots-binding-kind">Master 내부 호출</span>
                      <StateChip table={ROUTE_MODES} value={internal.mode} />
                    </div>
                    <p className="bots-note">
                      내부 호출에는 Slack 토큰이 없습니다. 라우팅 탭에서 바꿉니다.
                    </p>
                  </div>
                ) : null}
              </article>
            )
          })}
        </div>
      </Section>
    </>
  )
}

/**
 * 연결이 있는 워크스페이스 목록.
 *
 * 탭마다 따로 부르면 화면을 옮길 때마다 목록이 깜빡이고, 그 사이 고른 값이
 * 사라진다. 한 곳에서 읽어 넘긴다.
 */
function useWorkspaceKeys(): string[] {
  const bots = useResource<BotsResponse>('/api/bots')
  return useMemo(() => {
    const rows = bots.data?.bots ?? []
    return Array.from(
      new Set(rows.flatMap((bot) => bot.bindings.map((binding) => binding.workspace))),
    ).sort()
  }, [bots.data])
}

export function BotsPage({
  tab,
  query,
  navigate,
  onToast,
  runtime,
}: {
  tab: 'list' | 'connections' | 'routing' | 'manifests' | 'audit' | 'runtime'
  query: URLSearchParams
  navigate: (path: string) => void
  onToast: (message: string) => void
  /** 기존 전문 봇 관리 화면. 폐기하지 않고 런타임 영역으로 재사용한다(§12.1). */
  runtime?: ReactNode
  user?: ConsoleUser
}) {
  const workspace = query.get('workspace') ?? ''
  const bot = query.get('bot') ?? ''
  const knownWorkspaces = useWorkspaceKeys()
  return (
    <>
      <PageHead
        crumb="운영"
        title="봇 관리"
        note="Master · Archiving Bot · 전문 봇을 한 곳에서 봅니다. 봇 하나에 연결이 여럿입니다."
      />
      <nav className="bots-tabs" aria-label="봇 관리 탭">
        <a
          href="#/manage/bots"
          className={`bots-tab ${tab === 'list' ? 'is-active' : ''}`}
          aria-current={tab === 'list' ? 'page' : undefined}
        >
          봇 목록
        </a>
        <a
          href={`#/manage/bots/connections${workspace ? `?workspace=${workspace}` : ''}`}
          className={`bots-tab ${tab === 'connections' ? 'is-active' : ''}`}
          aria-current={tab === 'connections' ? 'page' : undefined}
        >
          워크스페이스 연결
        </a>
        <a
          href={`#/manage/bots/routing${workspace ? `?workspace=${workspace}` : ''}`}
          className={`bots-tab ${tab === 'routing' ? 'is-active' : ''}`}
          aria-current={tab === 'routing' ? 'page' : undefined}
        >
          라우팅
        </a>
        <a
          href={`#/manage/bots/manifests${workspace ? `?workspace=${workspace}` : ''}`}
          className={`bots-tab ${tab === 'manifests' ? 'is-active' : ''}`}
          aria-current={tab === 'manifests' ? 'page' : undefined}
        >
          Slack 앱 Manifest
        </a>
        <a
          href={`#/manage/bots/audit${workspace ? `?workspace=${workspace}` : ''}`}
          className={`bots-tab ${tab === 'audit' ? 'is-active' : ''}`}
          aria-current={tab === 'audit' ? 'page' : undefined}
        >
          변경 이력
        </a>
        <a
          href="#/manage/bots/runtime"
          className={`bots-tab ${tab === 'runtime' ? 'is-active' : ''}`}
          aria-current={tab === 'runtime' ? 'page' : undefined}
        >
          런타임
        </a>
      </nav>
      {tab === 'list' ? (
        <BotList
          navigate={navigate}
          onSelect={(key) => navigate(`/manage/bots?bot=${key}`)}
        />
      ) : tab === 'connections' ? (
        <WorkspaceConnections
          workspace={workspace}
          onToast={onToast}
          onWorkspaceChange={(key) => navigate(`/manage/bots/connections?workspace=${key}`)}
        />
      ) : tab === 'routing' ? (
        <BotRouting
          workspace={workspace}
          workspaces={knownWorkspaces}
          onToast={onToast}
          onWorkspaceChange={(key) => navigate(`/manage/bots/routing?workspace=${key}`)}
        />
      ) : tab === 'manifests' ? (
        <BotManifests
          workspace={workspace}
          workspaces={knownWorkspaces}
          onToast={onToast}
          onWorkspaceChange={(key) => navigate(`/manage/bots/manifests?workspace=${key}`)}
        />
      ) : tab === 'audit' ? (
        <BotAudit workspace={workspace} />
      ) : (
        runtime ?? null
      )}
      {bot ? <p className="bots-note">선택한 봇: {bot}</p> : null}
    </>
  )
}
