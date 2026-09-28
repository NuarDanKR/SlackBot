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
  /** 초대 여부. 수집 대상의 **정본**이다(2026-09-29). */
  membership?: 'joined' | 'left' | 'unknown'
  membership_checked_at?: string | null
  /** 사람이 끈 채널. 재초대해도 저절로 켜지지 않는다. */
  operator_hold?: boolean
  last_event_at?: string | null
  channel_name?: string
  is_private?: boolean
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

// 모드는 **무엇이 달라지는지**로 적는다. `off` 를 「수집 안 함」 으로만 적으면
// 사람은 원문이 안 쌓이는 줄 아는데, 실제로는 Master 가 그대로 쓴다.
const MODE_LABEL: Record<ChannelMode, string> = {
  off: '미적용',
  shadow: '그림자 수집',
  active: '운영 수집',
  paused: '일시 중지',
}

const MODE_DETAIL: Record<ChannelMode, string> = {
  off: 'Archiver 가 수집하지 않고 Master 가 운영본을 기록합니다.',
  shadow: 'Archiver 는 별도 shadow 경로에 기록하고 Master 가 운영본을 계속 기록합니다.',
  active: '현재 파일럿에서는 선택할 수 없습니다.',
  paused: '양쪽 writer 인수 상태를 확인한 뒤에만 사용합니다.',
}


//: 자동 등록될 때의 모드. 운영 수집으로는 **자동 전환하지 않는다**(오너 지시 12).
//
// 채널 상한은 없앴다 — 대상이 초대로 정해지므로, 상한을 두면 초대했는데 안 되는
// 채널이 생기고 그건 목록을 손으로 적던 때와 같은 고장이다(2026-09-29).
//: 이 화면에서 켤 수 없는 스위치. 켜는 순간 운영 원문의 모양이 바뀐다.
const PILOT_BLOCKED_FLAGS = ['archiver_writes_live', 'separate_attachments']

function modeChip(mode: ChannelMode) {
  if (mode === 'active') return <Chip tone="ok">{MODE_LABEL[mode]}</Chip>
  if (mode === 'shadow') return <Chip tone="watch">{MODE_LABEL[mode]}</Chip>
  if (mode === 'paused') return <Chip tone="stalled">{MODE_LABEL[mode]}</Chip>
  return <Chip tone="plain">{MODE_LABEL[mode]}</Chip>
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

  async function send(path: string, body: unknown): Promise<boolean> {
    setBusy(true)
    setError(null)
    try {
      setDetail(await api.put<ArchivingDetail>(path, body))
      return true
    } catch (caught) {
      // 422 는 고장이 아니라 **규칙이 막은 것**이다. 사유를 그대로 보여 준다.
      setError(caught instanceof ApiError ? caught.message : String(caught))
      return false
    } finally {
      setBusy(false)
    }
  }

  const base = `/api/workspaces/${encodeURIComponent(workspace)}/archiving`
  const gate = data.schemaGate
  const attachmentReaderReady = data.flags.some((row) =>
    row.name === 'attachment_reader_ready' && row.scope === 'global' && row.enabled,
  )

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
        title="봇 연결"
        lead="토큰 정본은 봇 관리입니다. 워크스페이스 하나에 Master·Archiver 가 각각 붙고, Slack 앱과 토큰은 워크스페이스마다 별도입니다."
      >
        <div className="card card-pad">
          <p className="field-help">
            이 화면에서는 토큰을 받지 않습니다.{' '}
            <a href={`#/manage/bots/connections?workspace=${encodeURIComponent(workspace)}`}>
              봇 관리 &gt; 워크스페이스 연결
            </a>
            에서 봇별로 등록하고 Slack 신원을 확인하세요. 여기서는 수집 설정만 다룹니다.
          </p>
        </div>
      </Section>

      <Section
        title="수집 채널"
        lead="Archiving Bot 을 채널에 초대하면 자동으로 목록에 들어오고 그림자 수집이 시작됩니다. 채널 ID 를 손으로 넣지 않습니다 — 초대가 정본입니다."
      >
        <div className="card card-pad">
          <p className="field-help">
            공개·비공개 채널 모두 됩니다. 채널 이름 규칙은 보지 않습니다. DM 은 수집하지
            않습니다. 봇을 채널에서 내보내면 수집이 멈추고 상태가 <strong>참여 아님</strong>
            으로 기록됩니다.
          </p>
          <p className="field-help">
            목록은 봇 기동 시점과 주기 동기화로 갱신됩니다. 방금 초대한 채널이 아직 안
            보이면 다음 동기화까지 기다리거나, 그 채널에 메시지가 오면 그때 등록됩니다.
          </p>
        </div>
      </Section>

      <Section
        title="채널 수집 모드"
        lead="그림자 수집은 운영 원문을 건드리지 않습니다 — Master 가 계속 운영본을 씁니다. 운영 수집(writer 인수)으로는 자동 전환하지 않습니다."
      >
        <div className="card card-pad"><div className="table-scroll"><table className="table">
          <thead><tr><th>채널</th><th>참여</th><th>수집 상태</th><th>마지막 이벤트</th>
            <th>운영 원문 주인</th><th className="right">수집</th></tr></thead>
          <tbody>
            {data.channels.map((row) => (
              <ChannelRowView
                key={row.channel_id} row={row} busy={busy}
                onHold={(hold, reason) => void send(
                  `${base}/channels/${encodeURIComponent(row.channel_id)}/hold`,
                  { hold, reason },
                )}
              />
            ))}
            {!data.channels.length && (
              <tr><td colSpan={6}>
                아직 초대된 채널이 없습니다. Slack 에서 Archiving Bot 을 채널에 초대하세요.
              </td></tr>
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
                key={`${row.name}:${row.scope}:${row.scope_key}`}
                pilotBlocked={PILOT_BLOCKED_FLAGS.includes(row.name)} row={row} busy={busy}
                gate={gate} gatedFlags={data.gatedFlags}
                dependencyLocked={row.name === 'separate_attachments' && !attachmentReaderReady}
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


function ChannelRowView({ row, busy, onHold }: {
  row: ChannelRow
  busy: boolean
  onHold: (hold: boolean, reason: string) => void
}) {
  const [reason, setReason] = useState('')
  const joined = (row.membership ?? 'unknown') === 'joined'
  const held = Boolean(row.operator_hold)
  const ready = reason.trim().length > 0 && !busy

  return (
    <tr>
      <td>
        <div className="mono">{row.channel_id}</div>
        {row.channel_name && <div className="hint">#{row.channel_name}</div>}
        {row.is_private && <div className="hint">비공개</div>}
      </td>
      <td>
        {/* 「참여 중」 과 「모른다」 는 다르다. 모르는 것을 참여로 읽으면 이미
            쫓겨난 채널을 계속 대상으로 본다. */}
        {joined
          ? <Chip tone="ok">참여 중</Chip>
          : row.membership === 'left'
            ? <Chip tone="stalled">참여 아님</Chip>
            : <Chip tone="plain">확인 전</Chip>}
        {row.membership_checked_at && (
          <div className="hint">{fmt.dayClock(row.membership_checked_at)} 확인</div>
        )}
      </td>
      <td>
        {modeChip(row.mode)}
        <div className="hint">
          {held
            ? '사람이 수집을 꺼 둔 채널입니다. 재초대해도 저절로 켜지지 않습니다.'
            : MODE_DETAIL[row.mode]}
        </div>
      </td>
      <td>
        {/* 프로세스가 떠 있다는 것과 이벤트가 들어왔다는 것은 다르다. */}
        {row.last_event_at
          ? <span className="mono">{fmt.dayClock(row.last_event_at)}</span>
          : <span className="hint">아직 없음</span>}
      </td>
      <td>{row.writer_owner === 'archiver' ? 'Archiver' : 'Master'}</td>
      <td className="right">
        <div className="field">
          <input className="input" placeholder="바꾸는 이유 (필수)" value={reason}
            disabled={busy} onChange={(event) => setReason(event.target.value)} />
          <button className="btn btn-sm" disabled={!ready}
            onClick={() => { onHold(!held, reason); setReason('') }}>
            {held ? '수집 재개' : '수집 중지'}
          </button>
          {!joined && !held && (
            <span className="field-help">
              봇이 이 채널에 없습니다. 다시 초대하면 그림자 수집으로 돌아옵니다.
            </span>
          )}
        </div>
      </td>
    </tr>
  )
}

function FlagRowView({
  row, busy, gate, gatedFlags, dependencyLocked, pilotBlocked, onChange,
}: {
  row: FlagRow
  busy: boolean
  gate: SchemaGate
  gatedFlags: string[]
  dependencyLocked: boolean
  /** 파일럿에서 켤 수 없는 스위치. 켜는 순간 운영 원문의 모양이 바뀐다. */
  pilotBlocked: boolean
  onChange: (enabled: boolean, reason: string) => void
}) {
  const [reason, setReason] = useState('')
  const next = !row.enabled
  // **끄는 것은 막지 않는다.** 사고 때 내리는 손잡이를 검증 상태로 막으면
  // 막아야 할 순간에 못 막는다.
  const schemaLocked = next && !gate.verified && gatedFlags.includes(row.name)
  const locked = schemaLocked || (next && dependencyLocked) || (next && pilotBlocked)
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
          {schemaLocked && <span className="field-help warn">스키마 검증 뒤에 켤 수 있습니다.</span>}
          {!schemaLocked && next && dependencyLocked && (
            <span className="field-help warn">첨부 reader 준비를 먼저 확인하세요.</span>
          )}
          {next && pilotBlocked && (
            <span className="field-help warn">
              파일럿 범위 밖입니다. 이 스위치는 shadow 검증을 마친 뒤 별도 승인으로 켭니다.
            </span>
          )}
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
