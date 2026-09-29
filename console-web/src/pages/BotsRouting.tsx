import { useMemo, useState } from 'react'
import { ApiError, api } from '../api/client'
import { useResource } from '../api/hooks'
import { Empty, Failed, Loading, Section } from '../components/primitives'
import { ROUTE_MODES, meaningOf, tooltipOf } from '../botModes'
import { RuntimeNotice } from './Bots'
import type { Runtime, RuntimeEffect } from './Bots'

/**
 * 라우팅 · Manifest · 변경 이력.
 *
 * 서버: `bot_admin.workspace_routes` · `bot_manifest` · `bot_admin.audit`
 *
 * ## 라우팅은 「누가 답하나」 다
 *
 * 라우트를 켠다고 그 봇이 Slack 에 붙는 것이 아니다. Master 가 내부 호출로 그 봇을
 * 부르고, 답을 사용자에게 **전달할지**를 정하는 것이다. 그림자는 부르되 전달하지
 * 않는다 — 대조만 한다.
 *
 * 그리고 지금은 그 설정이 **기록만 된다.** Master 는 아직 배정 표만 보고
 * 라우팅한다(`runtimeEffect.appliesNow === false`).
 */

type RouteRow = {
  key: string
  workspace: string
  mode: string
  fallbackBotKey: string
  assigned: boolean
  lastShadowCheckedAt: string | null
  lastShadowResult: string
  updatedAt: string | null
  updatedBy: string
  runtime: Runtime | null
}

type RoutesResponse = {
  workspace: string
  routes: RouteRow[]
  runtimeEffect: RuntimeEffect
}

type ManifestRow = {
  manifestId: string
  botKey: string
  connectorType: string
  sourcePath: string
  purpose: string
  sha256: string
  present: boolean
}

type AuditRow = {
  at: string
  actor: string
  subject: string
  workspace: string
  field: string
  oldValue: string
  newValue: string
  reason: string
}

const MODE_ORDER: Array<'disabled' | 'shadow' | 'active'> = ['disabled', 'shadow', 'active']

export function BotRouting({
  workspace,
  onToast,
  onWorkspaceChange,
  workspaces,
}: {
  workspace: string
  onToast: (message: string) => void
  onWorkspaceChange: (workspace: string) => void
  workspaces: string[]
}) {
  const resource = useResource<RoutesResponse>(
    workspace ? `/api/workspaces/${encodeURIComponent(workspace)}/bot-routes` : null,
    [workspace],
  )
  const [data, setData] = useState<RoutesResponse | null>(null)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState('')
  const view = data && data.workspace === workspace ? data : resource.data

  async function change(botKey: string, mode: string) {
    if (!reason.trim()) {
      onToast('변경 사유를 적어 주세요. 사고가 났을 때 범위를 정하려면 필요합니다.')
      return
    }
    setBusy(botKey)
    try {
      const next = await api.put<RoutesResponse>(
        `/api/workspaces/${encodeURIComponent(workspace)}/bot-routes/${botKey}`,
        { mode, reason },
      )
      setData(next)
      // 「전환했습니다」 라고 쓰지 않는다. 지금 답변 경로는 그대로다.
      onToast('라우팅 설정을 기록했습니다. 지금 답변 경로는 바뀌지 않습니다.')
      setReason('')
    } catch (error) {
      onToast(error instanceof ApiError ? error.message : '바꾸지 못했습니다.')
    } finally {
      setBusy('')
    }
  }

  if (!workspace) {
    return (
      <Section title="워크스페이스를 고르세요" lead="라우팅은 워크스페이스마다 다릅니다.">
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
  if (resource.loading && !view) return <Loading what="라우팅" />
  if (resource.error && !view) {
    return <Failed what="라우팅을" detail={resource.error?.message ?? ''} onRetry={resource.reload} />
  }
  if (!view) return null

  return (
    <>
      <RuntimeNotice effect={view.runtimeEffect} />
      <Section
        title={`${workspace} 내부 호출`}
        lead="Master 가 이 워크스페이스에서 어떤 전문 봇을 부르는지 정합니다. 그림자는 부르되 사용자에게 전달하지 않습니다."
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
        <label className="bots-reason">
          변경 사유
          <input
            className="input"
            value={reason}
            placeholder="예: PF 공존 대조 시작"
            onChange={(event) => setReason(event.target.value)}
          />
        </label>

        {view.routes.length === 0 ? (
          <Empty
            title="배정된 전문 봇이 없습니다"
            note="전문 봇 배정은 런타임 탭의 변경 요청으로 합니다. 배정이 없으면 라우팅할 대상도 없습니다."
          />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>봇</th>
                  <th>지금</th>
                  <th>런타임</th>
                  <th>바꾸기</th>
                </tr>
              </thead>
              <tbody>
                {view.routes.map((row) => {
                  const meaning = meaningOf(ROUTE_MODES, row.mode)
                  const healthy =
                    row.runtime?.state === 'enabled' && row.runtime?.health === 'ok'
                  return (
                    <tr key={row.key}>
                      <th scope="row">
                        {row.key}
                        {row.assigned ? null : (
                          <div className="bots-warn">이 워크스페이스에 배정 없음</div>
                        )}
                      </th>
                      <td>
                        <span className={`bots-chip tone-${meaning.tone}`} title={tooltipOf(meaning)}>
                          {meaning.label}
                        </span>
                        <div className="bots-sub">{meaning.detail}</div>
                      </td>
                      <td>
                        {row.runtime ? (
                          <span className={`bots-chip tone-${healthy ? 'good' : 'warn'}`}>
                            {healthy ? '사용 가능' : '점검 필요'}
                          </span>
                        ) : (
                          <span className="bots-muted">런타임 없음</span>
                        )}
                      </td>
                      <td>
                        <div className="bots-actions">
                          {MODE_ORDER.filter((mode) => mode !== row.mode).map((mode) => (
                            <button
                              key={mode}
                              type="button"
                              className="btn ghost btn-sm"
                              disabled={busy === row.key}
                              title={tooltipOf(meaningOf(ROUTE_MODES, mode))}
                              onClick={() => change(row.key, mode)}
                            >
                              {meaningOf(ROUTE_MODES, mode).label}
                            </button>
                          ))}
                        </div>
                        {row.mode !== 'active' && !healthy ? (
                          <div className="bots-sub">
                            실제 호출은 런타임이 사용 가능할 때만 켤 수 있습니다.
                          </div>
                        ) : null}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </Section>
    </>
  )
}

export function BotManifests({
  workspace,
  onToast,
  workspaces,
  onWorkspaceChange,
}: {
  workspace: string
  onToast: (message: string) => void
  workspaces: string[]
  onWorkspaceChange: (workspace: string) => void
}) {
  const manifests = useResource<{ manifests: ManifestRow[] }>('/api/bot-manifests')
  const [busy, setBusy] = useState('')
  const [reason, setReason] = useState('')

  async function attest(row: ManifestRow) {
    if (!workspace) {
      onToast('워크스페이스를 먼저 고르세요.')
      return
    }
    if (!reason.trim()) {
      onToast('대조 사유를 적어 주세요.')
      return
    }
    setBusy(row.manifestId)
    try {
      await api.put(
        `/api/workspaces/${encodeURIComponent(workspace)}/bot-connections/${row.botKey}/slack/manifest-attestation`,
        { manifestId: row.manifestId, sha256: row.sha256, reason },
      )
      // 대조는 **사람이 확인했다는 기록**이다. Slack 앱 설정이 바뀌지는 않는다.
      onToast('대조 결과를 기록했습니다. Slack 앱 설정이 바뀌지는 않습니다.')
      setReason('')
    } catch (error) {
      onToast(error instanceof ApiError ? error.message : '기록하지 못했습니다.')
    } finally {
      setBusy('')
    }
  }

  if (manifests.loading && !manifests.data) return <Loading what="Manifest" />
  if (manifests.error || !manifests.data) {
    return (
      <Failed
        what="Manifest 목록을"
        detail={manifests.error?.message ?? ''}
        onRetry={manifests.reload}
      />
    )
  }

  return (
    <Section
      title="Slack 앱 Manifest"
      lead="정본 파일의 hash 입니다. Slack 앱 설정과 직접 대조한 뒤, 확인했다는 사실을 연결에 남깁니다. 토큰 등록 시 자동 연결 확인과는 다른 검사입니다."
      aside={
        <select
          className="input"
          value={workspace}
          onChange={(event) => onWorkspaceChange(event.target.value)}
          aria-label="워크스페이스"
        >
          <option value="">워크스페이스 선택</option>
          {workspaces.map((key) => (
            <option key={key} value={key}>{key}</option>
          ))}
        </select>
      }
    >
      <p className="bots-note">
        `auth.test` 가 통과해도 Manifest 가 같다는 뜻은 아닙니다. 토큰은 맞는데 스코프가
        빠져 있으면 수집이 조용히 절반만 됩니다.
      </p>
      <label className="bots-reason">
        대조 사유
        <input
          className="input"
          value={reason}
          placeholder="예: 앱 설정 화면과 대조함"
          onChange={(event) => setReason(event.target.value)}
        />
      </label>
      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>봇</th>
              <th>용도</th>
              <th>정본 파일</th>
              <th>hash</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {manifests.data.manifests.map((row) => (
              <tr key={row.manifestId}>
                <th scope="row">{row.botKey}</th>
                <td>{row.purpose}</td>
                <td className="bots-mono">{row.sourcePath}</td>
                <td className="bots-mono">
                  {row.present ? `${row.sha256.slice(0, 12)}…` : '파일 없음'}
                </td>
                <td>
                  <button
                    type="button"
                    className="btn ghost btn-sm"
                    disabled={!row.present || busy === row.manifestId}
                    onClick={() => attest(row)}
                  >
                    대조 기록
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="bots-note">
        Hermes 의 PF 직접 연결 Manifest 는 PF 승인본이라 이 저장소에 없습니다. 목록에
        없는 것은 「아직 대조 대상이 아님」 입니다.
      </p>
    </Section>
  )
}

export function BotAudit({ workspace }: { workspace: string }) {
  const resource = useResource<{ entries: AuditRow[] }>(
    `/api/bot-audit?workspace=${encodeURIComponent(workspace)}&limit=100`,
    [workspace],
  )
  const entries = useMemo(() => resource.data?.entries ?? [], [resource.data])

  if (resource.loading && !resource.data) return <Loading what="변경 이력" />
  if (resource.error || !resource.data) {
    return (
      <Failed what="변경 이력을" detail={resource.error?.message ?? ''} onRetry={resource.reload} />
    )
  }

  return (
    <Section
      title="변경 이력"
      lead="연결·라우팅·옛 서비스 표의 변경이 한 줄기로 보입니다. 누가 언제 왜 바꿨는지가 없으면 사고가 났을 때 범위를 정할 수 없습니다."
    >
      {entries.length === 0 ? (
        <Empty title="기록이 없습니다" note="아직 이 워크스페이스에서 바꾼 것이 없습니다." />
      ) : (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>시각</th>
                <th>바꾼 사람</th>
                <th>대상</th>
                <th>무엇</th>
                <th>사유</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((row, index) => (
                <tr key={`${row.at}-${index}`}>
                  <td className="bots-mono">{String(row.at).slice(0, 19).replace('T', ' ')}</td>
                  <td>{row.actor}</td>
                  <td className="bots-mono">{row.field}</td>
                  <td>
                    {row.oldValue ? `${row.oldValue} → ` : ''}
                    {row.newValue}
                  </td>
                  <td>{row.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  )
}
