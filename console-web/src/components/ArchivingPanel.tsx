/** Archiving Bot 상세 — 워크스페이스 하나의 서비스·채널·스위치·보존·감사.
 *
 * 결정: 2026-09-25 오너 §4·§10.
 *
 * ## 이 화면이 지키는 것
 *
 * **막힌 것은 눌리기 전에 회색이다.** 서버가 거절하면 422 로 사유가 오지만,
 * 눌러 보고 거절당하는 것과 왜 못 누르는지 보이는 것은 다르다. 후자만 사람이
 * 무엇을 해야 하는지 안다.
 *
 * **사유 없이는 못 바꾼다.** 입력란이 비면 버튼이 안 눌린다. 서버도 막지만,
 * 서버까지 갔다 오면 사람은 「저장이 안 되네」 로 읽는다.
 *
 * **토큰은 mask 만 온다.** 서버가 평문을 주지 않으므로 여기서 가릴 것도 없다.
 */
import { useState } from 'react'
import { ApiError, api } from '../api/client'
import { useResource } from '../api/hooks'
import { Chip, Failed, Loading, Section, fmt } from './primitives'

export type ChannelMode = 'off' | 'shadow' | 'active' | 'paused'

export interface ServiceRow {
  service: 'master' | 'archiver' | 'hermes_direct'
  state: 'enabled' | 'disabled' | 'error'
  error: string | null
  team_id: string
  bot_user_id: string
  identity_ok: boolean | null
  identity_error: string
  identity_checked_at: string | null
  bot_mask: string | null
  app_mask: string | null
  token_count: number
}

export interface ChannelRow {
  channel_id: string
  mode: ChannelMode
  writer_owner: 'master' | 'archiver'
  cutover_ts: string
  cutover_at: string | null
  is_pilot: boolean
  note: string
  updated_at: string | null
  updated_by: string
}

export interface FlagRow {
  name: string
  scope: 'global' | 'workspace' | 'channel'
  scope_key: string
  enabled: boolean
  description: string
  updated_by: string
}

export interface RetentionRow {
  name: string
  retention_days: number | null
  approved_by: string
  approved_at: string | null
  description: string
}

export interface AuditRow {
  at: string
  actor: string
  subject: string
  channel_id: string
  field: string
  old_value: string
  new_value: string
  reason: string
}

export interface SchemaGate {
  verified: boolean
  reason: string
  currentFingerprint: string
  verifiedFingerprint: string
  verifiedAt: string
  verifiedBy: string
  verifiedDsnLabel: string
}

export interface ArchivingDetail {
  workspace: string
  services: ServiceRow[]
  channels: ChannelRow[]
  flags: FlagRow[]
  retention: RetentionRow[]
  audit: AuditRow[]
  schemaGate: SchemaGate
  blockers: string[]
  gatedModes: string[]
  gatedFlags: string[]
}

const SERVICE_LABEL: Record<ServiceRow['service'], string> = {
  master: 'Master (TYBot)',
  archiver: 'Archiver (수집)',
  hermes_direct: 'Hermes 직접 호출 (공존 기간)',
}

const MODE_LABEL: Record<ChannelMode, string> = {
  off: '수집 안 함',
  shadow: '그림자',
  active: '운영 수집',
  paused: '일시정지',
}

const MODES: ChannelMode[] = ['off', 'shadow', 'active', 'paused']

function modeChip(mode: ChannelMode) {
  if (mode === 'active') return <Chip tone="ok">{MODE_LABEL[mode]}</Chip>
  if (mode === 'shadow') return <Chip tone="watch">{MODE_LABEL[mode]}</Chip>
  if (mode === 'paused') return <Chip tone="stalled">{MODE_LABEL[mode]}</Chip>
  return <Chip tone="plain">{MODE_LABEL[mode]}</Chip>
}

function identityChip(row: ServiceRow) {
  // `null` 은 **아직 확인 안 했다** 다. `false`(확인했고 틀렸다)와 구분한다 —
  // 둘을 같이 보여 주면 사람이 「검사했는데 실패」 와 「검사를 안 함」 을 못 가린다.
  if (row.identity_ok === null) return <Chip tone="plain">신원 미확인</Chip>
  if (row.identity_ok) return <Chip tone="ok">신원 확인됨</Chip>
  return <Chip tone="stalled">신원 불일치</Chip>
}

export function ArchivingPanel({ workspace }: { workspace: string }) {
  const res = useResource<ArchivingDetail>(
    `/api/workspaces/${encodeURIComponent(workspace)}/archiving`,
    [workspace],
  )
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [detail, setDetail] = useState<ArchivingDetail | null>(null)

  const data = detail ?? res.data
  if (res.loading && !data) return <Loading what="Archiving 설정" />
  if (res.error && !data) return <Failed what="Archiving 설정" detail={res.error.message} onRetry={res.reload} />
  if (!data) return null

  async function send(path: string, body: unknown) {
    setBusy(true)
    setError(null)
    try {
      setDetail(await api.put<ArchivingDetail>(path, body))
    } catch (caught) {
      // 422 는 고장이 아니라 **규칙이 막은 것**이다. 사유를 그대로 보여 준다.
      setError(caught instanceof ApiError ? caught.message : String(caught))
    } finally {
      setBusy(false)
    }
  }

  const base = `/api/workspaces/${encodeURIComponent(workspace)}/archiving`
  const gate = data.schemaGate

  return (
    <>
      {!gate.verified && (
        <div className="notice warn">
          <div className="notice-kind">스키마 검증 미완료</div>
          <div>
            <div className="notice-title">운영 전환이 잠겨 있습니다</div>
            <div className="notice-detail">{gate.reason}</div>
            <div className="hint mono">
              지금 스키마 {gate.currentFingerprint}
              {gate.verifiedFingerprint && ` · 검증된 것 ${gate.verifiedFingerprint}`}
            </div>
          </div>
        </div>
      )}
      {gate.verified && (
        <div className="notice ok">
          <div className="notice-kind">스키마 검증 완료</div>
          <div>
            <div className="notice-title">
              {gate.verifiedDsnLabel || '격리 DB'}에서 확인 · {gate.verifiedBy}
            </div>
            <div className="hint mono">{gate.currentFingerprint}</div>
          </div>
        </div>
      )}

      {error && (
        <div className="notice bad">
          <div className="notice-kind">거절됨</div>
          <div>
            <div className="notice-title">설정을 바꾸지 못했습니다</div>
            <div className="notice-detail">{error}</div>
          </div>
        </div>
      )}

      {data.blockers.length > 0 && (
        <div className="notice warn">
          <div className="notice-kind">운영 전환 전 남은 것</div>
          <div>
            {data.blockers.map((item) => (
              <div key={item} className="notice-detail">{item}</div>
            ))}
          </div>
        </div>
      )}

      <Section
        title="서비스 연결"
        lead="워크스페이스는 하나이고 여기 붙는 서비스가 여럿입니다. 토큰은 가린 값만 표시됩니다."
      >
        <div className="card card-pad"><div className="table-scroll"><table className="table">
          <thead><tr><th>서비스</th><th>상태</th><th>토큰</th><th>Slack 신원</th></tr></thead>
          <tbody>
            {data.services.map((row) => (
              <tr key={row.service}>
                <td>{SERVICE_LABEL[row.service]}</td>
                <td>
                  {row.state === 'enabled'
                    ? <Chip tone="ok">연결됨</Chip>
                    : <Chip tone="plain">{row.state === 'error' ? '오류' : '중지'}</Chip>}
                  {row.error && <div className="hint warn">{row.error}</div>}
                </td>
                <td>
                  <div className="mono">{row.bot_mask ?? '미등록'}</div>
                  <div className="mono">{row.app_mask ?? '미등록'}</div>
                </td>
                <td>
                  {identityChip(row)}
                  <div className="hint mono">
                    {row.team_id || '-'} · {row.bot_user_id || '-'}
                  </div>
                  {row.identity_error && <div className="hint warn">{row.identity_error}</div>}
                  {row.identity_checked_at && (
                    <div className="hint">{fmt.dayClock(row.identity_checked_at)}</div>
                  )}
                </td>
              </tr>
            ))}
            {!data.services.length && (
              <tr><td colSpan={4}>등록된 서비스가 없습니다. Master 토큰을 먼저 이관하세요.</td></tr>
            )}
          </tbody>
        </table></div></div>
      </Section>

      <Section
        title="채널 수집 모드"
        lead="그림자는 운영 원문을 건드리지 않습니다. 운영 수집으로 넘기려면 인수 시각이 필요합니다."
      >
        <div className="card card-pad"><div className="table-scroll"><table className="table">
          <thead><tr><th>채널</th><th>모드</th><th>운영 원문 주인</th><th>인수 시각</th>
            <th className="right">바꾸기</th></tr></thead>
          <tbody>
            {data.channels.map((row) => (
              <ChannelRowView
                key={row.channel_id} row={row} busy={busy} gate={gate}
                gatedModes={data.gatedModes}
                onChange={(mode, reason, cutoverTs) => void send(
                  `${base}/channels/${encodeURIComponent(row.channel_id)}`,
                  { mode, reason, cutoverTs },
                )}
              />
            ))}
            {!data.channels.length && (
              <tr><td colSpan={5}>등록된 채널이 없습니다.</td></tr>
            )}
          </tbody>
        </table></div></div>
      </Section>

      <Section
        title="기능 스위치"
        lead="켜는 것은 스키마 검증 뒤에만 됩니다. 끄는 것은 언제나 됩니다 — 사고 때 내릴 손잡이입니다."
      >
        <div className="card card-pad"><div className="table-scroll"><table className="table">
          <thead><tr><th>스위치</th><th>범위</th><th>상태</th><th className="right">바꾸기</th></tr></thead>
          <tbody>
            {data.flags.map((row) => (
              <FlagRowView
                key={`${row.name}:${row.scope}:${row.scope_key}`} row={row} busy={busy}
                gate={gate} gatedFlags={data.gatedFlags}
                onChange={(enabled, reason) => void send(`${base}/flags`, {
                  name: row.name, enabled, reason, scope: row.scope, scopeKey: row.scope_key,
                })}
              />
            ))}
          </tbody>
        </table></div></div>
      </Section>

      <Section
        title="보존 정책"
        lead="값이 없으면 기본이 영구 보관이 됩니다. 정하기 전에는 운영 전환이 막힙니다."
      >
        <div className="card card-pad"><div className="table-scroll"><table className="table">
          <thead><tr><th>정책</th><th>보존</th><th>승인</th><th className="right">바꾸기</th></tr></thead>
          <tbody>
            {data.retention.map((row) => (
              <RetentionRowView
                key={row.name} row={row} busy={busy}
                onChange={(days, reason) => void send(`${base}/retention`, {
                  name: row.name, days, reason,
                })}
              />
            ))}
          </tbody>
        </table></div></div>
      </Section>

      <Section title="최근 설정 변경" lead="누가 언제 왜 바꿨는지. 지워지지 않습니다.">
        <div className="card card-pad"><div className="table-scroll"><table className="table">
          <thead><tr><th>시각</th><th>바꾼 사람</th><th>대상</th><th>변경</th><th>사유</th></tr></thead>
          <tbody>
            {data.audit.map((row, index) => (
              <tr key={`${row.at}:${index}`}>
                <td>{fmt.dayClock(row.at)}</td>
                <td>{row.actor}</td>
                <td>
                  <div>{row.field}</div>
                  {row.channel_id && <div className="hint mono">{row.channel_id}</div>}
                </td>
                <td className="mono">{row.old_value || '-'} → {row.new_value || '-'}</td>
                <td>{row.reason}</td>
              </tr>
            ))}
            {!data.audit.length && <tr><td colSpan={5}>변경 기록이 없습니다.</td></tr>}
          </tbody>
        </table></div></div>
      </Section>
    </>
  )
}

function ChannelRowView({ row, busy, gate, gatedModes, onChange }: {
  row: ChannelRow
  busy: boolean
  gate: SchemaGate
  gatedModes: string[]
  onChange: (mode: ChannelMode, reason: string, cutoverTs: string) => void
}) {
  const [mode, setMode] = useState<ChannelMode>(row.mode)
  const [reason, setReason] = useState('')
  const [cutover, setCutover] = useState('')

  const locked = !gate.verified && gatedModes.includes(mode)
  // 인수·역인수는 좌표가 필요하다. 없으면 서버가 거절하는데, 그걸 눌러 보고
  // 알게 하지 않는다.
  const needsCutover =
    (mode === 'active' && row.mode !== 'active') ||
    (mode === 'shadow' && row.writer_owner === 'archiver')
  const ready = mode !== row.mode && reason.trim().length > 0
    && (!needsCutover || cutover.trim().length > 0) && !locked && !busy

  return (
    <tr>
      <td>
        <div className="mono">{row.channel_id}</div>
        {row.is_pilot && <div className="hint">파일럿</div>}
      </td>
      <td>{modeChip(row.mode)}</td>
      <td>{row.writer_owner === 'archiver' ? 'Archiver' : 'Master'}</td>
      <td className="mono">{row.cutover_ts || '-'}</td>
      <td className="right">
        <div className="field">
          <select className="input" value={mode} disabled={busy}
            onChange={(event) => setMode(event.target.value as ChannelMode)}>
            {MODES.map((value) => (
              <option key={value} value={value}
                disabled={!gate.verified && gatedModes.includes(value)}>
                {MODE_LABEL[value]}
                {!gate.verified && gatedModes.includes(value) ? ' (검증 필요)' : ''}
              </option>
            ))}
          </select>
          {needsCutover && (
            <input className="input mono" placeholder="인수 시각 (Slack ts)"
              value={cutover} disabled={busy}
              onChange={(event) => setCutover(event.target.value)} />
          )}
          <input className="input" placeholder="바꾸는 이유 (필수)" value={reason}
            disabled={busy} onChange={(event) => setReason(event.target.value)} />
          <button className="btn btn-sm" disabled={!ready}
            onClick={() => onChange(mode, reason, cutover)}>적용</button>
          {locked && <span className="field-help warn">스키마 검증 뒤에 열립니다.</span>}
        </div>
      </td>
    </tr>
  )
}

function FlagRowView({ row, busy, gate, gatedFlags, onChange }: {
  row: FlagRow
  busy: boolean
  gate: SchemaGate
  gatedFlags: string[]
  onChange: (enabled: boolean, reason: string) => void
}) {
  const [reason, setReason] = useState('')
  const next = !row.enabled
  // **끄는 것은 막지 않는다.** 사고 때 내리는 손잡이를 검증 상태로 막으면
  // 막아야 할 순간에 못 막는다.
  const locked = next && !gate.verified && gatedFlags.includes(row.name)
  const ready = reason.trim().length > 0 && !locked && !busy

  return (
    <tr>
      <td>
        <div>{row.name}</div>
        {row.description && <div className="hint">{row.description}</div>}
      </td>
      <td>
        {row.scope === 'global' ? '전역' : row.scope === 'workspace' ? '워크스페이스' : '채널'}
        {row.scope_key && <div className="hint mono">{row.scope_key}</div>}
      </td>
      <td>{row.enabled ? <Chip tone="ok">켜짐</Chip> : <Chip tone="plain">꺼짐</Chip>}</td>
      <td className="right">
        <div className="field">
          <input className="input" placeholder="바꾸는 이유 (필수)" value={reason}
            disabled={busy} onChange={(event) => setReason(event.target.value)} />
          <button className="btn btn-sm" disabled={!ready}
            onClick={() => onChange(next, reason)}>
            {next ? '켜기' : '끄기'}
          </button>
          {locked && <span className="field-help warn">스키마 검증 뒤에 켤 수 있습니다.</span>}
        </div>
      </td>
    </tr>
  )
}

function RetentionRowView({ row, busy, onChange }: {
  row: RetentionRow
  busy: boolean
  onChange: (days: number | null, reason: string) => void
}) {
  const [days, setDays] = useState(row.retention_days === null ? '' : String(row.retention_days))
  const [reason, setReason] = useState('')
  const parsed = days.trim() === '' ? null : Number(days)
  // 0 과 음수를 여기서 막는다. 서버도 막지만, 서버까지 갔다 오면 사람은
  // 「저장이 안 되네」 로 읽는다.
  const valid = parsed === null || (Number.isInteger(parsed) && parsed >= 1)
  const ready = valid && reason.trim().length > 0 && !busy

  return (
    <tr>
      <td>
        <div>{row.name}</div>
        {row.description && <div className="hint">{row.description}</div>}
      </td>
      <td>
        {row.retention_days === null
          ? <Chip tone="stalled">안 정함</Chip>
          : <span>{row.retention_days}일</span>}
      </td>
      <td>
        {row.approved_by || '-'}
        {row.approved_at && <div className="hint">{fmt.dayClock(row.approved_at)}</div>}
      </td>
      <td className="right">
        <div className="field">
          <input className="input" inputMode="numeric" placeholder="일수 (비우면 안 정함)"
            value={days} disabled={busy}
            onChange={(event) => setDays(event.target.value)} />
          <input className="input" placeholder="바꾸는 이유 (필수)" value={reason}
            disabled={busy} onChange={(event) => setReason(event.target.value)} />
          <button className="btn btn-sm" disabled={!ready}
            onClick={() => onChange(parsed, reason)}>저장</button>
          {!valid && <span className="field-help warn">1일 이상이어야 합니다.</span>}
        </div>
      </td>
    </tr>
  )
}
