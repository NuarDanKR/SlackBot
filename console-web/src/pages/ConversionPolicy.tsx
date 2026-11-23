import { useEffect, useState } from 'react'
import { ApiError, api } from '../api/client'
import { useResource } from '../api/hooks'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { Chip, Failed, Loading, Section, fmt } from '../components/primitives'

/** 서버 `ConversionPolicy.to_json(include_actor=True)` 와 같은 모양입니다. */
export interface ConversionPolicyState {
  state: 'db' | 'legacy' | 'unknown'
  reason: string
  delegation: 'on' | 'off' | 'unknown'
  appIds: string[]
  recheckLimit: number | null
  rhwpGraceHours: number | null
  sources: Record<string, string>
  revision: number | null
  nightlyApplyAllowed: boolean
  hwpDirectConversion: boolean
  updatedBy?: string
  updatedAt?: string
}

interface HistoryEntry {
  revision: number
  appIds: string[]
  recheckLimit: number
  rhwpGraceHours: number
  updatedBy: string
  reason: string
  recordedAt: string
}

interface PolicyView {
  policy: ConversionPolicyState
  legacy: { appIds: string; appIdsSource: string; recheckLimit: string; rhwpGraceHours: string }
  history: HistoryEntry[]
  historyError: string
  limits: { recheckLimit: [number, number]; rhwpGraceHours: [number, number]; maxAppIds: number }
  defaults: { recheckLimit: number; rhwpGraceHours: number }
  timerNote: string
}

const STATE_LABEL: Record<ConversionPolicyState['state'], { tone: 'ok' | 'watch' | 'bad'; text: string }> = {
  db: { tone: 'ok', text: 'DB 정본' },
  legacy: { tone: 'watch', text: '전환 중 · 옛 설정' },
  unknown: { tone: 'bad', text: '알 수 없음' },
}

const REASON_TEXT: Record<string, string> = {
  db_unconfigured: 'DB 접속 정보가 없습니다',
  db_unavailable: 'DB 에 접속하지 못했습니다',
  table_missing: '정책 표가 없거나 권한이 없습니다 (스키마 적용 필요)',
  row_missing: '아직 저장된 정책이 없습니다',
  invalid_value: 'DB 값이 올바르지 않습니다',
}

const SOURCE_TEXT: Record<string, string> = {
  db: 'DB',
  'legacy-console-file': '옛 콘솔 설정 파일',
  'legacy-env': '옛 환경변수',
  default: '기본값',
  unknown: '모름',
}

const DELEGATION_TEXT: Record<ConversionPolicyState['delegation'], string> = {
  on: '켜짐 — rhwp 가 올린 PDF 만 받고 HWP 는 직접 변환하지 않음',
  off: '꺼짐 — TYBot 이 HWP/HWPX 를 직접 변환',
  unknown: '모름 — HWP/HWPX 직접 변환과 새벽 재점검 실행을 멈춤',
}

interface Draft {
  appIds: string
  recheckLimit: number
  rhwpGraceHours: number
  reason: string
}

function initialDraft(view: PolicyView): Draft {
  const { policy, legacy, defaults } = view
  // DB 값이 없으면 옛 설정 값을 채워 둡니다. 그대로 저장하면 DB 로 옮겨집니다.
  const legacyLimit = Number.parseInt(legacy.recheckLimit, 10)
  const legacyGrace = Number.parseInt(legacy.rhwpGraceHours, 10)
  return {
    appIds: policy.state === 'db' ? policy.appIds.join(', ') : (policy.appIds.join(', ') || legacy.appIds),
    recheckLimit: policy.state === 'db' && policy.recheckLimit !== null
      ? policy.recheckLimit : (Number.isFinite(legacyLimit) ? legacyLimit : defaults.recheckLimit),
    rhwpGraceHours: policy.state === 'db' && policy.rhwpGraceHours !== null
      ? policy.rhwpGraceHours : (Number.isFinite(legacyGrace) ? legacyGrace : defaults.rhwpGraceHours),
    reason: '',
  }
}

function splitIds(raw: string): string[] {
  return raw.split(',').map((part) => part.trim()).filter(Boolean)
}

export function ConversionPolicyPanel({ onToast }: { onToast: (message: string) => void }) {
  const resource = useResource<PolicyView>('/api/conversion-policy')
  const [draft, setDraft] = useState<Draft | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [confirm, setConfirm] = useState(false)

  useEffect(() => {
    if (resource.data) setDraft(initialDraft(resource.data))
  }, [resource.data])

  if (resource.loading && !resource.data) return <Loading what="변환 정책을" />
  if (resource.error && !resource.data) {
    return <Failed what="변환 정책을" detail={resource.error.message} onRetry={resource.reload} />
  }
  const view = resource.data
  if (!view || !draft) return null
  const { policy, limits } = view
  const label = STATE_LABEL[policy.state]
  const ids = splitIds(draft.appIds)
  const inRange = (value: number, [low, high]: [number, number]) => Number.isInteger(value) && value >= low && value <= high
  const valid = ids.length <= limits.maxAppIds
    && inRange(draft.recheckLimit, limits.recheckLimit)
    && inRange(draft.rhwpGraceHours, limits.rhwpGraceHours)
    && draft.reason.trim().length > 0

  async function save() {
    if (!draft || saving) return
    setSaving(true)
    setError(null)
    try {
      await api.put('/api/conversion-policy', {
        rhwpAppIds: splitIds(draft.appIds),
        recheckLimit: draft.recheckLimit,
        rhwpGraceHours: draft.rhwpGraceHours,
        expectedRevision: policy.state === 'db' ? policy.revision ?? 0 : 0,
        reason: draft.reason.trim(),
      })
      resource.reload()
      onToast('변환 정책을 저장했습니다. 1분 안에 수집·변환에 반영됩니다. 새벽 타이머는 따로 켜야 합니다.')
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Section
      title="변환 정책 (rhwp 위임 · 새벽 재점검)"
      lead={
        'DB 한 행이 정본입니다. 값을 모르면(DB 장애·행 없음·잘못된 값) 위임 꺼짐으로 보지 않고 ' +
        'HWP/HWPX 직접 변환과 새벽 재점검 실행을 멈춥니다. 앱 ID 와 숫자만 다루며 시크릿은 저장하지 않습니다.'
      }
    >
      <div className="card card-pad">
        <div className="form-row">
          <Chip tone={label.tone}>{label.text}</Chip>
          {policy.reason && <span className="hint">{REASON_TEXT[policy.reason] ?? policy.reason}</span>}
          {policy.revision !== null && <span className="hint mono">판 {policy.revision}</span>}
          {policy.updatedAt && <span className="hint">{fmt.dayClock(policy.updatedAt)} · {policy.updatedBy}</span>}
        </div>
        <div className="table-wrap">
          <table className="table">
            <tbody>
              <tr><th>rhwp 위임</th><td>{DELEGATION_TEXT[policy.delegation]}</td></tr>
              <tr><th>앱 ID</th><td><span className="mono">{policy.appIds.join(', ') || '없음'}</span>
                <span className="hint"> · 출처 {SOURCE_TEXT[policy.sources.appIds] ?? policy.sources.appIds}</span></td></tr>
              <tr><th>한 밤 소급 상한</th><td>{policy.recheckLimit ?? '모름'}
                <span className="hint"> · 출처 {SOURCE_TEXT[policy.sources.recheckLimit] ?? policy.sources.recheckLimit}</span></td></tr>
              <tr><th>rhwp PDF 대기</th><td>{policy.rhwpGraceHours !== null ? `${policy.rhwpGraceHours}시간` : '모름'}
                <span className="hint"> · 출처 {SOURCE_TEXT[policy.sources.rhwpGraceHours] ?? policy.sources.rhwpGraceHours}</span></td></tr>
              <tr><th>HWP/HWPX 직접 변환</th><td>{policy.hwpDirectConversion ? '허용' : '막힘'}</td></tr>
              <tr><th>새벽 재점검 실행</th><td>{policy.nightlyApplyAllowed ? '가능' : '거절 (DB 정본이 아님)'}</td></tr>
            </tbody>
          </table>
        </div>
        {policy.state === 'legacy' && (
          <div className="notice watch"><div className="notice-kind">전환 중</div>
            <div>옛 설정 값으로 동작하고 있습니다. 아래 칸에 옛 값을 채워 두었으니 확인 후 저장하면 DB 로 옮겨집니다.</div></div>
        )}
        <div className="notice"><div className="notice-kind">타이머</div><div>{view.timerNote}</div></div>
      </div>

      <div className="card card-pad model-setting">
        <div className="field">
          <label className="field-label" htmlFor="policy-app-ids">rhwp Slack 앱 ID</label>
          <input id="policy-app-ids" className="input mono" type="text" placeholder="A0123456789"
            value={draft.appIds} onChange={(event) => setDraft({ ...draft, appIds: event.target.value })} />
          <span className="field-help">
            지정한 앱이 채널에 올린 <b>PDF 파일만</b> 원문 첨부로 받고 TYBot 은 HWP 를 직접 변환하지 않습니다.
            <b>비워서 저장하면 위임을 끄고</b> TYBot 이 HWP 를 직접 변환합니다. 쉼표로 {limits.maxAppIds}개까지.
          </span>
        </div>
        <div className="field">
          <label className="field-label" htmlFor="policy-recheck-limit">한 밤 소급 상한 (건)</label>
          <input id="policy-recheck-limit" className="input" type="number" step={1}
            min={limits.recheckLimit[0]} max={limits.recheckLimit[1]} value={draft.recheckLimit}
            onChange={(event) => setDraft({ ...draft, recheckLimit: Number(event.target.value) })} />
          <span className="field-help">{limits.recheckLimit[0]}~{limits.recheckLimit[1]} · 판정 변경·구버전 재변환을 합친 상한. 0 은 소급하지 않음.</span>
        </div>
        <div className="field">
          <label className="field-label" htmlFor="policy-grace">rhwp PDF 대기 (시간)</label>
          <input id="policy-grace" className="input" type="number" step={1}
            min={limits.rhwpGraceHours[0]} max={limits.rhwpGraceHours[1]} value={draft.rhwpGraceHours}
            onChange={(event) => setDraft({ ...draft, rhwpGraceHours: Number(event.target.value) })} />
          <span className="field-help">{limits.rhwpGraceHours[0]}~{limits.rhwpGraceHours[1]} · 이 시간이 지나도 PDF 가 없으면 「미확인」 으로 셉니다.</span>
        </div>
        <div className="field">
          <label className="field-label" htmlFor="policy-reason">변경 사유 (감사 기록)</label>
          <input id="policy-reason" className="input" type="text" maxLength={500}
            value={draft.reason} onChange={(event) => setDraft({ ...draft, reason: event.target.value })} />
        </div>
        {error && <div className="notice bad"><div className="notice-kind">저장 실패</div><div>{error}</div></div>}
        <div className="form-row">
          <button className="btn btn-primary" disabled={!valid || saving} onClick={() => setConfirm(true)}>
            {saving ? '저장 중…' : '변환 정책 저장'}
          </button>
        </div>
      </div>

      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>판</th><th>앱 ID</th><th>소급 상한</th><th>대기</th><th>실행자 · 사유</th><th>시각</th></tr></thead>
          <tbody>
            {view.history.length === 0 && (
              <tr><td colSpan={6} className="hint">{view.historyError ? `이력을 읽지 못했습니다 (${view.historyError})` : '변경 이력이 없습니다.'}</td></tr>
            )}
            {view.history.map((entry) => (
              <tr key={entry.revision}>
                <td className="mono">{entry.revision}</td>
                <td className="mono">{entry.appIds.join(', ') || '없음(위임 꺼짐)'}</td>
                <td>{entry.recheckLimit}</td>
                <td>{entry.rhwpGraceHours}시간</td>
                <td>{entry.updatedBy}<div className="hint">{entry.reason}</div></td>
                <td>{fmt.dayClock(entry.recordedAt)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <ConfirmDialog open={confirm} title="변환 정책을 저장할까요?"
        detail={ids.length
          ? `rhwp 위임을 켭니다(앱 ${ids.length}개). TYBot 은 HWP/HWPX 를 직접 변환하지 않습니다. 새벽 타이머는 켜지지 않습니다.`
          : 'rhwp 위임을 끕니다. TYBot 이 HWP/HWPX 를 직접 변환합니다. 새벽 타이머는 켜지지 않습니다.'}
        confirmLabel="저장" busy={saving} onCancel={() => setConfirm(false)}
        onConfirm={() => { void save().finally(() => setConfirm(false)) }} />
    </Section>
  )
}
