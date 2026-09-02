import { useEffect, useMemo, useState } from 'react'
import { ApiError, api } from '../api/client'
import { useResource } from '../api/hooks'
import { Chip, Failed, Loading, PageHead, Section, fmt } from '../components/primitives'

interface EnvSettingsData {
  path: string
  editable: boolean
  reason: string | null
  restartPending: boolean
  realtimeIngest: boolean
  autojoinChannels: boolean
  replyInThread: boolean
  changed?: string[]
}

function copySettings(data: EnvSettingsData): EnvSettingsData {
  return { ...data }
}

function preview(data: EnvSettingsData): string {
  return [
    `REALTIME_INGEST=${data.realtimeIngest ? '1' : '0'}`,
    `AUTOJOIN_CHANNELS=${data.autojoinChannels ? '1' : '0'}`,
    `REPLY_IN_THREAD=${data.replyInThread ? '1' : '0'}`,
  ].join('\n')
}

interface LlmSecret {
  provider: string
  envName: string
  mask: string
  enabled: boolean
  updatedAt: string | null
  updatedBy: string
  /** DB 에 없고 아직 환경변수를 쓰고 있습니다. */
  inEnv: boolean
}

const PROVIDER_LABEL: Record<string, string> = { anthropic: 'Claude (Anthropic)', openai: 'GPT (OpenAI)' }

export function EnvSettings({ onToast }: { onToast: (message: string) => void }) {
  const secrets = useResource<{ secrets: LlmSecret[] }>('/api/llm-secrets')
  const [keyDraft, setKeyDraft] = useState<Record<string, string>>({})
  const [keyBusy, setKeyBusy] = useState<string | null>(null)
  const [keyError, setKeyError] = useState<string | null>(null)
  const resource = useResource<EnvSettingsData>('/api/env-settings')
  const [draft, setDraft] = useState<EnvSettingsData | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)

  useEffect(() => {
    if (resource.data) setDraft(copySettings(resource.data))
  }, [resource.data])

  // 키 값은 화면 상태에만 잠깐 머문다. 보낸 뒤 지운다 — 남겨 두면 다른 탭으로
  // 옮겼다 돌아왔을 때도 입력칸에 평문이 남아 있다.
  async function saveKey(provider: string) {
    const key = (keyDraft[provider] ?? "").trim()
    if (!key) return
    setKeyBusy(provider); setKeyError(null)
    try {
      await api.put('/api/llm-secrets', { provider, key })
      setKeyDraft((prev) => ({ ...prev, [provider]: "" }))
      secrets.reload()
      onToast('키를 저장했습니다. 다음 답변부터 적용됩니다.')
    } catch (caught) {
      setKeyError(caught instanceof ApiError ? caught.message : String(caught))
    } finally { setKeyBusy(null) }
  }

  async function toggleKey(provider: string, enabled: boolean) {
    setKeyBusy(provider); setKeyError(null)
    try {
      await api.put('/api/llm-secrets', { provider, key: '', enabled })
      secrets.reload()
    } catch (caught) {
      setKeyError(caught instanceof ApiError ? caught.message : String(caught))
    } finally { setKeyBusy(null) }
  }

  const renderedPreview = useMemo(() => (draft ? preview(draft) : ''), [draft])

  if (resource.loading && !draft) return <Loading what="환경변수 설정을" />
  if (resource.error && !draft) {
    return <Failed what="환경변수 설정을" detail={resource.error.message} onRetry={resource.reload} />
  }
  if (!draft) return null

  async function save() {
    if (!draft || !draft.editable || saving) return
    if (!window.confirm('설정을 저장하고 TYBot 재시작을 요청하시겠습니까?')) return
    setSaving(true)
    setSaveError(null)
    try {
      const saved = await api.put<EnvSettingsData>('/api/env-settings', {
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
        note="봇의 공통 동작과 LLM API 키를 관리합니다. 워크스페이스와 Slack 토큰은 워크스페이스 관리에서 설정합니다."
        aside={draft.restartPending ? <Chip tone="watch">재시작 대기</Chip> : <Chip tone="ok">적용 상태 정상</Chip>}
      />

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
        title="LLM API 키"
        lead={
          '키는 암호화해서 DB 에 저장합니다. 암호화 키는 DB 밖 파일에 있어 DB 백업만으로는 풀 수 없습니다. ' +
          '저장한 값은 다시 볼 수 없고 가린 값만 보입니다. 삭제 대신 사용 중지를 지원합니다.'
        }
      >
        {keyError && <div className="notice bad"><div className="notice-kind">저장 실패</div><div>{keyError}</div></div>}
        <div className="table-wrap">
          <table className="table">
            <thead><tr><th>프로바이더</th><th>현재 키</th><th>새 키</th><th>관리</th></tr></thead>
            <tbody>
              {(secrets.data?.secrets ?? []).map((s) => (
                <tr key={s.provider}>
                  <td>{PROVIDER_LABEL[s.provider] ?? s.provider}
                    <div className="hint mono">{s.envName}</div></td>
                  <td>
                    {s.mask
                      ? <><div className="mono">{s.mask}</div>
                          <div className="hint">{s.updatedAt ? fmt.dayClock(s.updatedAt) : "-"} · {s.updatedBy}</div></>
                      : s.inEnv
                        ? <Chip tone="watch">환경변수 사용</Chip>
                        : <Chip tone="bad">없음</Chip>}
                  </td>
                  <td>
                    <input className="input mono" type="password" autoComplete="new-password"
                      placeholder="새 키를 붙여 넣으면 교체됩니다"
                      value={keyDraft[s.provider] ?? ""}
                      onChange={(e) => setKeyDraft((p) => ({ ...p, [s.provider]: e.target.value }))} />
                  </td>
                  <td className="nowrap">
                    <button className="btn btn-sm btn-primary" disabled={keyBusy === s.provider || !(keyDraft[s.provider] ?? "").trim()}
                      onClick={() => saveKey(s.provider)}>저장</button>{" "}
                    {s.mask && (
                      <button className="btn btn-sm" disabled={keyBusy === s.provider}
                        onClick={() => toggleKey(s.provider, !s.enabled)}>
                        {s.enabled ? "사용 중지" : "사용"}</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
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
