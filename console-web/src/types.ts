/** 콘솔이 다루는 데이터 모양.
 *
 * 필드 이름은 **다음 턴에 붙일 실제 API 응답과 1:1로 맞춰 둡니다.**
 * 지금은 목데이터가 들어가지만, 배선할 때 컴포넌트를 고치지 않아도 되게 하는 것이 목적입니다.
 *
 * ## 콘솔이 받지 않는 것
 *   - 사용자 **질문 본문**. 사용량 표에는 의도·근거건수·모델·비용만 남깁니다.
 *   - **시크릿 원문**. 서버는 마스킹된 문자열만 내려줍니다. 복호화 조회 API 는 두지 않습니다.
 *
 * ## 조건부로만 받는 것 — 아카이브 원문 본문 (`CollectedDoc.content`)
 *   수집이 제대로 되는지 눈으로 확인해야 하므로 열람 화면을 둡니다. 다만 원문은
 *   Slack 채널 멤버십(권한 3층)으로 보호되던 자료이므로 다음 세 조건을 함께 겁니다.
 *     1. `role === 'admin'` 인 사용자에게만 `content` 를 내려줍니다. 나머지는 목록·메타데이터만 봅니다.
 *     2. 열람 자체를 감사 기록에 남깁니다(누가·어느 문서를·언제).
 *     3. 화면에도 그 사실을 표시합니다 — 조용히 열리는 경로를 만들지 않습니다.
 */

export type WorkspaceRole = 'root' | 'member'
export type Health = 'ok' | 'watch' | 'stalled'

/** 적층도의 한 칸 — 하루치 타설량. lines 가 0 이면 그날은 '구멍'이다. */
export interface DailyCourse {
  date: string
  lines: number
}

export interface WorkspaceStatus {
  key: string
  label: string
  role: WorkspaceRole
  /** 이 워크스페이스가 추가로 읽을 수 있는 워크스페이스 키 (CROSS_WS_READ) */
  readable: string[]
  connected: boolean
  realtime: boolean
  channels: number
  /** 봇이 초대되지 않아 수집 대상에서 빠진 채널 수 */
  uninvitedChannels: number
  docs: number
  rawLines: number
  brokenDocs: number
  lastIngestedAt: string | null
  /** 아카이브·감사기록 쓰기 실패 사유. null 이면 정상 */
  writeProblem: string | null
  courses: DailyCourse[]
  spendTodayUsd: number
  limitUsd: number
  health: Health
}

export type AnomalyKind = 'spike' | 'limit' | 'loop' | 'stalled'
export type AnomalyState = 'open' | 'ack' | 'breaker'

export interface Anomaly {
  id: string
  workspace: string
  kind: AnomalyKind
  detectedAt: string
  /** 기준선 대비 배수. 3.0 이면 평소의 세 배 */
  factor: number
  headline: string
  detail: string
  state: AnomalyState
}

export interface ModelSpend {
  model: string
  calls: number
  inputTokens: number
  outputTokens: number
  costUsd: number
}

export interface WorkspaceSpend {
  key: string
  label: string
  calls: number
  costUsd: number
  limitUsd: number
}

/** 최근 호출 — 질문 본문은 담지 않는다. */
export interface CallRow {
  at: string
  /** 같은 시각대의 서비스 로그를 찾기 위한 전체 타임스탬프. */
  logAt: string
  workspace: string
  intent: string
  source: 'llm' | 'regex' | 'cmd'
  reason: string
  hits: number
  model: string
  costUsd: number
  ms: number
}

export interface UsageSnapshot {
  /** 집계 기준 시각 (KST) */
  asOf: string
  limitUsd: number
  spentUsd: number
  /** 현 속도 유지 시 자정 예상치 */
  projectedUsd: number
  /** 최근 14일 같은 시각 중위값 — 이상 판정의 기준선 */
  baselineUsd: number
  callsToday: number
  byHour: { hour: string; calls: number; costUsd: number }[]
  byModel: ModelSpend[]
  byWorkspace: WorkspaceSpend[]
  recent: CallRow[]
}

export interface Capabilities {
  specialists: boolean
  approvedSummaries: boolean
  summaryReview: boolean
}

export interface AuditEvent {
  id: string
  at: string
  actor: string
  category: string
  action: string
  targetType: string
  targetId: string
  workspace: string | null
  outcome: 'requested' | 'succeeded' | 'failed' | string
  source: 'database' | 'legacy'
  metadata: Record<string, unknown>
}

export interface Specialist {
  key: string
  name: string
  domain: string
  adapter: string
  adapterAvailable: boolean
  state: 'draft' | 'enabled' | 'disabled' | 'error'
  version: string
  contractVersion: string
  health: 'unknown' | 'ok' | 'error'
  errorCode: string
  lastCheckedAt: string | null
  workspaces: string[]
  updatedAt: string
  updatedBy: string
}

export interface SpecialistCall {
  id: string
  at: string
  workspace: string
  specialist: string
  routingReason: string
  confidence: number | null
  result: 'success' | 'fallback' | 'error' | 'contract_violation'
  elapsedMs: number
  costUsd: number
  errorCode: string
}

export interface SpecialistRequest {
  id: string
  specialist: string
  proposal: Record<string, unknown>
  checks: Check[]
  requester: string
  requestedAt: string
  state: 'awaiting_approval' | 'approved' | 'rejected'
  approver: string | null
  decidedAt: string | null
  note: string
}

export type CheckId = 'pytest' | 'ruff' | 'schema' | 'secrets'
export type CheckState = 'pass' | 'fail' | 'running' | 'pending'

export interface Check {
  id: CheckId
  label: string
  state: CheckState
  detail: string
}

export type DeployState =
  | 'awaiting_checks'
  | 'awaiting_approval'
  | 'blocked'
  | 'approved'
  | 'applying'
  | 'live'
  | 'rejected'
  | 'rolled_back'

export interface DeployRequest {
  id: string
  workspace: string
  workspaceLabel: string
  requester: string
  requestedAt: string
  repo: string
  branch: string
  commit: string
  commitTitle: string
  author: string
  /** fast-forward 가 아니면 적용하지 않는다 (force·rebase 금지) */
  fastForward: boolean
  filesChanged: { path: string; added: number; removed: number }[]
  checks: Check[]
  state: DeployState
  /** 승인 유효시각. 지나면 재승인이 필요하다 */
  approvalExpiresAt: string | null
  approver: string | null
}

export interface DeployEvent {
  id: string
  at: string
  workspace: string
  commit: string
  actor: string
  action: '요청' | '승인' | '반려' | '적용' | '롤백'
  note: string
}

export type RegistryState = 'enabled' | 'disabled' | 'error'

export interface RegistryEntry {
  key: string
  label: string
  role: WorkspaceRole
  readable: string[]
  /** 마스킹된 값만. 원문은 서버에서도 복호화 조회를 열지 않는다 */
  botTokenMask: string
  appTokenMask: string
  secretUpdatedAt: string
  secretUpdatedBy: string
  state: RegistryState
  /** state === 'error' 일 때 사유. 이 워크스페이스만 멈추고 나머지는 계속 뜬다 */
  error: string | null
  limitUsd: number
  archivePath: string
  createdAt: string
  createdBy: string
}

/* ===== 콘솔 사용자 ===== */

/** guest는 조회, developer는 변경 요청, admin은 승인과 전체 설정을 담당합니다. */
export type ConsoleRole = 'guest' | 'developer' | 'admin'

export interface ConsoleUser {
  name: string
  email: string
  role: ConsoleRole
  /** 이 사용자가 다룰 수 있는 워크스페이스. admin은 전체입니다. */
  workspaces: string[]
}

/* ===== 봇 하네싱 (규칙 · 워크플로 MD) ===== */

/** 봇 동작을 정하는 MD 파일. 파이썬 코드가 아니라 규칙 문서입니다. */
export type HarnessKind = 'rules' | 'workflow' | 'glossary' | 'prompt'

export interface HarnessFile {
  workspace: string
  workspaceLabel: string
  path: string
  title: string
  kind: HarnessKind
  updatedAt: string
  updatedBy: string
  /** 승인 대기 중인 편집이 있으면 그 요청 id */
  pendingRequestId: string | null
  content: string
}

export interface HarnessRequest {
  id: string
  workspace: string
  workspaceLabel: string
  path: string
  title: string
  requester: string
  requestedAt: string
  /** 요청자가 직접 쓴 변경 이유 */
  reason: string
  added: number
  removed: number
  checks: Check[]
  state: 'awaiting_checks' | 'awaiting_approval' | 'blocked' | 'approved' | 'rejected'
  before: string
  after: string
}

/* ===== 수집된 아카이브 문서 열람 ===== */

/** 수집 문서 1건. `content` 는 **어드민에게만** 내려옵니다. */
export interface CollectedDoc {
  workspace: string
  workspaceLabel: string
  channel: string
  path: string
  lines: number
  bytes: number
  lastIngestedAt: string
  visibility: 'public' | 'private'
  acl: string[]
  shareWith: string[]
  /** 형식 검사 실패 사유. null 이면 정상 */
  schemaError: string | null
  /** 첨부 변환으로 들어온 줄 수 */
  attachmentLines: number
  content: string | null
}

/** 헬스 체크 — 서버 `src/tybot/console/health.py` 와 짝이 맞아야 합니다. */
export type HealthLevel = 'ok' | 'warn' | 'bad' | 'unknown'

export type HealthProblem = { section: string; message: string }

export type HealthReport = {
  level: HealthLevel
  days: number
  checkedAt: string
  problems: HealthProblem[]
  sections: {
    bot: {
      level: HealthLevel
      workspaces: {
        workspace: string
        label: string
        level: HealthLevel
        /** 상태 파일이 낡으면 null 입니다 — 끊긴 것과 구분합니다. */
        connected: boolean | null
        problems: string[]
      }[]
    }
    archive: {
      level: HealthLevel
      documents: number
      brokenDocuments: number
      staleWorkspaces: number
      problems: string[]
    }
    answers: {
      level: HealthLevel
      questions: number
      grounded?: number
      noHits?: number
      groundedRate?: number
      errors?: number
      errorRate?: number
      slowAnswers?: number
      topReasons?: { reason: string; count: number }[]
      /** 어떤 예외로 실패했는지. 예외 클래스명만 담깁니다(업무 내용 없음). */
      topErrors?: { kind: string; count: number }[]
      note?: string
      problems: string[]
    }
    commands: {
      level: HealthLevel
      commands: { name: string; inCode: boolean; inManifest: boolean }[]
      note?: string
      problems: string[]
    }
    feedback: {
      level: HealthLevel
      positive: number
      negative: number
      missing: number
      corrections: number
      rated: number
      /** 표본이 적으면 null 입니다. 0% 와 구분해야 합니다. */
      satisfaction: number | null
      note?: string
      problems: string[]
      /** 아직 처리하지 않은 신고 건수입니다. */
      openCorrections: number
      /** 신고 하나하나. 본문(text)은 관리자에게만 채워집니다. */
      items: {
        id: string
        at: string
        workspace: string
        kind: string
        actor: string
        name: string
        dept: string
        qaRecordId: string
        text: string
        hasText: boolean
        handled: boolean
        handledBy: string
        handledAt: string
        handledNote: string
      }[]
      /** 정정 사항을 많이 보낸 순. 본문은 관리자에게만 채워집니다. */
      contributors: {
        actor: string
        name: string
        dept: string
        workspaces: string[]
        corrections: number
        reports: number
        praise: number
        total: number
        lastCorrection: string
      }[]
    }
  }
}
