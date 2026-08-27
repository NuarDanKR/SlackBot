import { useEffect, useMemo, useState } from 'react'
import { ApiError, api } from '../api/client'
import { useResource } from '../api/hooks'
import { Chip, Failed, Loading, PageHead, Section } from '../components/primitives'

interface EnvWorkspace {
  key: string
  label: string
  root: boolean
  readable: string[]
}

interface EnvSettingsData {
  path: string
  editable: boolean
  reason: string | null
  restartPending: boolean
  workspaces: EnvWorkspace[]
  realtimeIngest: boolean
  autojoinChannels: boolean
  replyInThread: boolean
  changed?: string[]
}

function copySettings(data: EnvSettingsData): EnvSettingsData {
  return { ...data, workspaces: data.workspaces.map((w) => ({ ...w, readable: [...w.readable] })) }
}

function preview(data: EnvSettingsData): string {
  const roots = data.workspaces.filter((w) => w.root).map((w) => w.key)
  const cross = data.workspaces
    .filter((w) => w.readable.length)
    .map((w) => `${w.key}:${w.readable.join('|')}`)
  return [
    `WORKSPACES=${data.workspaces.map((w) => w.key).join(',')}`,
    `ROOT_WORKSPACES=${roots.join(',')}`,
    `CROSS_WS_READ=${cross.join(',')}`,
    `REALTIME_INGEST=${data.realtimeIngest ? '1' : '0'}`,
    `AUTOJOIN_CHANNELS=${data.autojoinChannels ? '1' : '0'}`,
    `REPLY_IN_THREAD=${data.replyInThread ? '1' : '0'}`,
    ...data.workspaces.map((w) => `WORKSPACE_LABEL_${w.key.replace(/[^a-z0-9]+/gi, '_').toUpperCase()}=${w.label}`),
  ].join('\n')
}

export function EnvSettings({ onToast }: { onToast: (message: string) => void }) {
  const resource = useResource<EnvSettingsData>('/api/env-settings')
  const [draft, setDraft] = useState<EnvSettingsData | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)

  useEffect(() => {
    if (resource.data) setDraft(copySettings(resource.data))
  }, [resource.data])

  const renderedPreview = useMemo(() => (draft ? preview(draft) : ''), [draft])

  if (resource.loading && !draft) return <Loading what="환경변수 설정을" />
  if (resource.error && !draft) {
    return <Failed what="환경변수 설정을" detail={resource.error.message} onRetry={resource.reload} />
  }
  if (!draft) return null

  function updateWorkspace(key: string, update: Partial<EnvWorkspace>) {
    setDraft((current) =>
      current
        ? {
            ...current,
            workspaces: current.workspaces.map((w) => (w.key === key ? { ...w, ...update } : w)),
          }
        : current,
    )
  }

  function toggleReadable(reader: string, target: string, checked: boolean) {
    const row = draft?.workspaces.find((w) => w.key === reader)
    if (!row) return
    const readable = checked
      ? [...new Set([...row.readable, target])]
      : row.readable.filter((key) => key !== target)
    updateWorkspace(reader, { readable })
  }

  async function save() {
    if (!draft || !draft.editable || saving) return
    if (!window.confirm('설정을 저장하고 TYBot 재시작을 요청하시겠습니까?')) return
    setSaving(true)
    setSaveError(null)
    try {
      const saved = await api.put<EnvSettingsData>('/api/env-settings', {
        workspaces: draft.workspaces,
        realtimeIngest: draft.realtimeIngest,
        autojoinChannels: draft.autojoinChannels,
        replyInThread: draft.replyInThread,
      })
      setDraft(copySettings(saved))
      const count = saved.changed?.length ?? 0
      onToast(count ? `환경변수 ${count}개를 저장했습니다. TYBot이 1분 안에 재시작됩니다.` : '변경된 값이 없습니다.')
    } catch (error) {
      setSaveError(error instanceof ApiError ? error.message : String(error))
    } finally {
      setSaving(false)
    }
  }

  return (
    <>
      <PageHead
        crumb="봇 관리 · 환경변수 설정"
        title="환경변수 설정"
        note="워크스페이스 관계와 봇 동작 설정을 관리합니다. Slack 토큰과 API 키는 이 화면에서 읽거나 표시하지 않습니다."
        aside={draft.restartPending ? <Chip tone="watch">재시작 대기</Chip> : <Chip tone="ok">적용 상태 정상</Chip>}
      />

      {!draft.editable && (
        <div className="notice warn">
          <div className="notice-kind">편집 불가</div>
          <div>
            <div className="notice-title">멀티 워크스페이스 설정이 필요합니다</div>
            <div className="notice-detail">{draft.reason}</div>
          </div>
        </div>
      )}

      {saveError && (
        <div className="notice bad">
          <div className="notice-kind">저장 실패</div>
          <div>
            <div className="notice-title">환경변수를 저장하지 못했습니다</div>
            <div className="notice-detail">{saveError}</div>
          </div>
        </div>
      )}

      <Section
        title="봇 동작"
        lead="변경 사항은 저장 후 봇이 재시작되면서 적용됩니다. 기존 아카이브 원문에는 영향을 주지 않습니다."
      >
        <div className="card card-pad env-toggle-grid">
          <label className="check-line">
            <input
              type="checkbox"
              checked={draft.realtimeIngest}
              disabled={!draft.editable}
              onChange={(e) => setDraft({ ...draft, realtimeIngest: e.target.checked })}
            />
            <span><b>실시간 수집</b><span className="field-help">규칙에 맞고 봇이 참여한 채널의 새 대화를 저장합니다.</span></span>
          </label>
          <label className="check-line">
            <input
              type="checkbox"
              checked={draft.autojoinChannels}
              disabled={!draft.editable}
              onChange={(e) => setDraft({ ...draft, autojoinChannels: e.target.checked })}
            />
            <span><b>공개 채널 자동 참여</b><span className="field-help">표준 두문자로 시작하는 공개 채널에 TYBot이 참여합니다.</span></span>
          </label>
          <label className="check-line">
            <input
              type="checkbox"
              checked={draft.replyInThread}
              disabled={!draft.editable}
              onChange={(e) => setDraft({ ...draft, replyInThread: e.target.checked })}
            />
            <span><b>스레드로 답변</b><span className="field-help">채널 본문 대신 질문 메시지의 스레드에 답합니다.</span></span>
          </label>
        </div>
      </Section>

      <Section
        title="워크스페이스 관계"
        lead="상위 지정은 권한의 종류이고, 열람 대상은 실제로 접근할 수 있는 화이트리스트입니다. 둘을 각각 설정합니다."
      >
        <div className="card card-pad">
          <div className="table-scroll">
            <table className="table env-table">
              <thead><tr><th>키</th><th>표시 이름</th><th>상위</th><th>크로스 열람 대상</th></tr></thead>
              <tbody>
                {draft.workspaces.map((workspace) => (
                  <tr key={workspace.key}>
                    <td className="mono">{workspace.key}</td>
                    <td>
                      <input
                        className="input"
                        value={workspace.label}
                        disabled={!draft.editable}
                        onChange={(e) => updateWorkspace(workspace.key, { label: e.target.value })}
                      />
                    </td>
                    <td>
                      <input
                        type="checkbox"
                        aria-label={`${workspace.label} 상위 워크스페이스`}
                        checked={workspace.root}
                        disabled={!draft.editable}
                        onChange={(e) => updateWorkspace(workspace.key, { root: e.target.checked })}
                      />
                    </td>
                    <td>
                      <div className="env-readable">
                        {draft.workspaces.filter((target) => target.key !== workspace.key).map((target) => (
                          <label className="check-line compact" key={target.key}>
                            <input
                              type="checkbox"
                              checked={workspace.readable.includes(target.key)}
                              disabled={!draft.editable}
                              onChange={(e) => toggleReadable(workspace.key, target.key, e.target.checked)}
                            />
                            <span>{target.label}</span>
                          </label>
                        ))}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </Section>

      <Section title="저장될 값" lead="시크릿은 포함되지 않습니다. 이 값만 관리 오버레이 파일에 기록됩니다.">
        <pre className="code-block env-preview">{renderedPreview}</pre>
        <div className="form-row">
          <button className="btn btn-primary" disabled={!draft.editable || saving} onClick={save}>
            {saving ? '저장 중…' : '저장하고 봇 재시작'}
          </button>
          <span className="field-help mono">{draft.path}</span>
        </div>
      </Section>
    </>
  )
}
