/** 목데이터 — 실제 배선 전까지 화면을 채운다.
 *
 * 숫자는 그럴듯하게가 아니라 **운영에서 실제로 나올 수 있는 모양**으로 짰다:
 * 주말에는 타설이 얇고, 봇을 초대하지 않은 채널이 남아 있고,
 * 한 워크스페이스는 실시간 수집이 3일째 끊겨 있다(= 옛 자료로 답하는 상태).
 */
import type {
  Anomaly,
  ConsoleRole,
  ConsoleUser,
  DailyCourse,
  DeployEvent,
  DeployRequest,
  RegistryEntry,
  UsageSnapshot,
  WorkspaceStatus,
} from '../types'

export const TODAY = '2026-08-21'

/** 결정적 의사난수 — 새로고침마다 그래프가 춤추지 않게 */
function seeded(seed: number) {
  let s = seed
  return () => {
    s = (s * 1103515245 + 12345) & 0x7fffffff
    return s / 0x7fffffff
  }
}

function courses(seed: number, days: number, opts: { base: number; gapTail?: number }): DailyCourse[] {
  const rnd = seeded(seed)
  const out: DailyCourse[] = []
  const end = new Date(`${TODAY}T00:00:00Z`)
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(end)
    d.setUTCDate(d.getUTCDate() - i)
    const date = d.toISOString().slice(0, 10)
    const dow = d.getUTCDay()
    const weekend = dow === 0 || dow === 6
    // 꼬리 구간은 수집이 끊긴 상태를 만든다
    const stalled = opts.gapTail !== undefined && i < opts.gapTail
    const lines = stalled
      ? 0
      : Math.round(opts.base * (weekend ? 0.12 : 0.7 + rnd() * 0.9))
    out.push({ date, lines })
  }
  return out
}

export const workspaces: WorkspaceStatus[] = [
  {
    key: 'mgmt',
    label: '경영본부',
    role: 'root',
    readable: ['pilot', 'site-gimhae', 'fin'],
    connected: true,
    realtime: true,
    channels: 18,
    uninvitedChannels: 2,
    docs: 18,
    rawLines: 7412,
    brokenDocs: 0,
    lastIngestedAt: '2026-08-21T14:26:00+09:00',
    writeProblem: null,
    courses: courses(11, 30, { base: 260 }),
    spendTodayUsd: 1.84,
    limitUsd: 4,
    health: 'ok',
  },
  {
    key: 'fin',
    label: '자금팀',
    role: 'member',
    readable: [],
    connected: true,
    realtime: true,
    channels: 7,
    uninvitedChannels: 0,
    docs: 7,
    rawLines: 3180,
    brokenDocs: 1,
    lastIngestedAt: '2026-08-21T13:58:00+09:00',
    writeProblem: null,
    courses: courses(23, 30, { base: 140 }),
    spendTodayUsd: 0.62,
    limitUsd: 2,
    health: 'watch',
  },
  {
    key: 'pilot',
    label: '파일럿',
    role: 'member',
    readable: [],
    connected: true,
    realtime: true,
    channels: 5,
    uninvitedChannels: 1,
    docs: 5,
    rawLines: 1244,
    brokenDocs: 0,
    lastIngestedAt: '2026-08-21T11:40:00+09:00',
    writeProblem: null,
    courses: courses(37, 30, { base: 70 }),
    spendTodayUsd: 0.21,
    limitUsd: 2,
    health: 'ok',
  },
  {
    key: 'site-gimhae',
    label: '현장 김해외동(180182)',
    role: 'member',
    readable: [],
    connected: true,
    realtime: false,
    channels: 9,
    uninvitedChannels: 4,
    docs: 9,
    rawLines: 2065,
    brokenDocs: 0,
    lastIngestedAt: '2026-08-18T17:12:00+09:00',
    writeProblem: null,
    courses: courses(59, 30, { base: 180, gapTail: 3 }),
    spendTodayUsd: 0,
    limitUsd: 2,
    health: 'stalled',
  },
  {
    key: 'safety',
    label: '안전관리팀',
    role: 'member',
    readable: [],
    connected: false,
    realtime: true,
    channels: 0,
    uninvitedChannels: 0,
    docs: 0,
    rawLines: 0,
    brokenDocs: 0,
    lastIngestedAt: null,
    writeProblem: '/var/lib/tybot/archive (PermissionError: 권한 없음)',
    courses: courses(71, 30, { base: 0, gapTail: 30 }),
    spendTodayUsd: 0,
    limitUsd: 2,
    health: 'stalled',
  },
]

export const anomalies: Anomaly[] = [
  {
    id: 'an-3391',
    workspace: 'fin',
    kind: 'spike',
    detectedAt: '2026-08-21T10:12:00+09:00',
    factor: 4.6,
    headline: '자금팀 호출이 기준선의 4.6배',
    detail: '10:00–10:12 사이 요약 요청 34건. 같은 사용자가 같은 질문을 반복했다.',
    state: 'ack',
  },
  {
    id: 'an-3392',
    workspace: 'site-gimhae',
    kind: 'stalled',
    detectedAt: '2026-08-21T09:00:00+09:00',
    factor: 0,
    headline: '현장 김해외동 수집이 3일째 없음',
    detail: '마지막 원문 08-18 17:12. 실시간 수집이 꺼져 있고 백필도 돌지 않았다.',
    state: 'open',
  },
  {
    id: 'an-3390',
    workspace: 'mgmt',
    kind: 'limit',
    detectedAt: '2026-08-20T16:44:00+09:00',
    factor: 1.0,
    headline: '경영본부가 일 상한의 92%에 도달',
    detail: '상한 $4.00 중 $3.68 사용. 자동 차단 없이 자정에 초기화됐다.',
    state: 'ack',
  },
]

const hours = Array.from({ length: 15 }, (_, i) => {
  const h = 7 + i
  const rnd = seeded(500 + i)()
  const calls = h < 9 ? Math.round(2 + rnd * 4) : h === 10 ? 41 : Math.round(6 + rnd * 18)
  return { hour: `${String(h).padStart(2, '0')}:00`, calls, costUsd: +(calls * 0.021).toFixed(3) }
})

export const usage: UsageSnapshot = {
  asOf: '2026-08-21T14:30:00+09:00',
  limitUsd: 10,
  spentUsd: 2.67,
  projectedUsd: 4.12,
  baselineUsd: 1.55,
  callsToday: hours.reduce((a, h) => a + h.calls, 0),
  byHour: hours,
  byModel: [
    {
      model: 'claude-sonnet-5',
      calls: 96,
      inputTokens: 742_000,
      outputTokens: 61_400,
      costUsd: 2.1,
    },
    {
      model: 'claude-haiku-4-5',
      calls: 214,
      inputTokens: 128_000,
      outputTokens: 9_800,
      costUsd: 0.177,
    },
    { model: 'claude-opus-4-8', calls: 3, inputTokens: 22_400, outputTokens: 4_100, costUsd: 0.393 },
  ],
  byWorkspace: [
    { key: 'mgmt', label: '경영본부', calls: 96, costUsd: 1.84, limitUsd: 4 },
    { key: 'fin', label: '자금팀', calls: 78, costUsd: 0.62, limitUsd: 2 },
    { key: 'pilot', label: '파일럿', calls: 38, costUsd: 0.21, limitUsd: 2 },
    { key: 'site-gimhae', label: '현장 김해외동(180182)', calls: 3, costUsd: 0, limitUsd: 2 },
    { key: 'safety', label: '안전관리팀', calls: 0, costUsd: 0, limitUsd: 2 },
  ],
  recent: [
    { at: '14:28', workspace: 'mgmt', intent: 'summary', source: 'llm', reason: 'answered', hits: 62, model: 'claude-sonnet-5', costUsd: 0.041, ms: 4120 },
    { at: '14:24', workspace: 'fin', intent: 'search', source: 'llm', reason: 'no_hits', hits: 0, model: '-', costUsd: 0.0003, ms: 640 },
    { at: '14:19', workspace: 'mgmt', intent: 'advice', source: 'llm', reason: 'advice', hits: 12, model: 'claude-sonnet-5', costUsd: 0.028, ms: 3380 },
    { at: '14:11', workspace: 'pilot', intent: 'status', source: 'regex', reason: 'status', hits: 0, model: '-', costUsd: 0, ms: 88 },
    { at: '14:02', workspace: 'fin', intent: 'search', source: 'llm', reason: 'answered', hits: 8, model: 'claude-sonnet-5', costUsd: 0.019, ms: 2740 },
    { at: '13:51', workspace: 'mgmt', intent: 'ingest', source: 'cmd', reason: 'ingest', hits: 0, model: '-', costUsd: 0, ms: 9210 },
    { at: '13:44', workspace: 'pilot', intent: 'out_of_scope', source: 'llm', reason: 'out_of_scope', hits: 0, model: '-', costUsd: 0.0003, ms: 520 },
    { at: '13:36', workspace: 'fin', intent: 'summary', source: 'llm', reason: 'answered', hits: 48, model: 'claude-opus-4-8', costUsd: 0.214, ms: 7860 },
  ],
}

export const deployRequests: DeployRequest[] = [
  {
    id: 'dp-118',
    workspace: 'fin',
    workspaceLabel: '자금팀',
    requester: '김수현',
    requestedAt: '2026-08-21T13:20:00+09:00',
    repo: 'taeyoung/tybot-fin',
    branch: 'main',
    commit: '4c1f9ab',
    commitTitle: '주간보고 요약에 기성금 항목 분리 추가',
    author: '김수현 <sh.kim@taeyoung.com>',
    fastForward: true,
    filesChanged: [
      { path: 'src/tybot/answer.py', added: 34, removed: 6 },
      { path: 'tests/test_answer.py', added: 41, removed: 0 },
      { path: 'docs/pilot/README.md', added: 5, removed: 2 },
    ],
    checks: [
      { id: 'pytest', label: '테스트', state: 'pass', detail: '154 passed · 12.4s' },
      { id: 'ruff', label: '포맷·린트', state: 'pass', detail: '지적 없음' },
      { id: 'schema', label: '아카이브 스키마', state: 'pass', detail: '문서 39건 전부 통과' },
      { id: 'secrets', label: '시크릿 유출', state: 'pass', detail: '토큰 패턴 없음' },
    ],
    state: 'awaiting_approval',
    approvalExpiresAt: null,
    approver: null,
  },
  {
    id: 'dp-119',
    workspace: 'site-gimhae',
    workspaceLabel: '현장 김해외동(180182)',
    requester: '박정호',
    requestedAt: '2026-08-21T14:05:00+09:00',
    repo: 'taeyoung/tybot-site-gimhae',
    branch: 'main',
    commit: '9be07d2',
    commitTitle: '현장 사진 첨부를 OCR로 읽어 원문에 넣기',
    author: '박정호 <jh.park@taeyoung.com>',
    fastForward: true,
    filesChanged: [
      { path: 'src/tybot/archive/convert.py', added: 88, removed: 3 },
      { path: 'requirements.txt', added: 2, removed: 0 },
    ],
    checks: [
      { id: 'pytest', label: '테스트', state: 'fail', detail: 'test_convert.py 2건 실패 — 스캔 PDF가 변환됨' },
      { id: 'ruff', label: '포맷·린트', state: 'pass', detail: '지적 없음' },
      { id: 'schema', label: '아카이브 스키마', state: 'pass', detail: '문서 39건 전부 통과' },
      { id: 'secrets', label: '시크릿 유출', state: 'pass', detail: '토큰 패턴 없음' },
    ],
    state: 'blocked',
    approvalExpiresAt: null,
    approver: null,
  },
  {
    id: 'dp-120',
    workspace: 'pilot',
    workspaceLabel: '파일럿',
    requester: '이서연',
    requestedAt: '2026-08-21T14:22:00+09:00',
    repo: 'taeyoung/tybot-pilot',
    branch: 'main',
    commit: 'a03e5c7',
    commitTitle: '도움말 문구 정리',
    author: '이서연 <sy.lee@taeyoung.com>',
    fastForward: true,
    filesChanged: [{ path: 'src/tybot/slack/pilot.py', added: 7, removed: 7 }],
    checks: [
      { id: 'pytest', label: '테스트', state: 'running', detail: '실행 중 · 78/154' },
      { id: 'ruff', label: '포맷·린트', state: 'pass', detail: '지적 없음' },
      { id: 'schema', label: '아카이브 스키마', state: 'pending', detail: '테스트 통과 후 실행' },
      { id: 'secrets', label: '시크릿 유출', state: 'pass', detail: '토큰 패턴 없음' },
    ],
    state: 'awaiting_checks',
    approvalExpiresAt: null,
    approver: null,
  },
]

export const deployHistory: DeployEvent[] = [
  { id: 'ev-441', at: '2026-08-21T09:41:00+09:00', workspace: 'mgmt', commit: 'e77b105', actor: '류대안', action: '적용', note: '헬스체크 통과 · 봇 재기동 6.2s' },
  { id: 'ev-440', at: '2026-08-21T09:38:00+09:00', workspace: 'mgmt', commit: 'e77b105', actor: '류대안', action: '승인', note: '검사 4건 통과' },
  { id: 'ev-439', at: '2026-08-20T18:02:00+09:00', workspace: 'fin', commit: '2ad9f31', actor: '시스템', action: '롤백', note: '적용 후 헬스체크 90초 실패 → 직전 커밋 c19f884 로 복구' },
  { id: 'ev-438', at: '2026-08-20T17:59:00+09:00', workspace: 'fin', commit: '2ad9f31', actor: '류대안', action: '적용', note: '수동 적용' },
  { id: 'ev-437', at: '2026-08-20T15:20:00+09:00', workspace: 'site-gimhae', commit: '55c0e18', actor: '류대안', action: '반려', note: '아카이브 원문을 덮어쓰는 코드 — 원칙 1 위반' },
]

export const registry: RegistryEntry[] = [
  {
    key: 'mgmt',
    label: '경영본부',
    role: 'root',
    readable: ['pilot', 'site-gimhae', 'fin'],
    botTokenMask: 'xoxb-4821…9f0c',
    appTokenMask: 'xapp-1a77…22de',
    secretUpdatedAt: '2026-08-12T10:04:00+09:00',
    secretUpdatedBy: '류대안',
    state: 'enabled',
    error: null,
    limitUsd: 4,
    archivePath: '/var/lib/tybot/archive/channels/mgmt',
    createdAt: '2026-08-04T09:00:00+09:00',
    createdBy: '류대안',
  },
  {
    key: 'fin',
    label: '자금팀',
    role: 'member',
    readable: [],
    botTokenMask: 'xoxb-7710…3b41',
    appTokenMask: 'xapp-9c02…7ea5',
    secretUpdatedAt: '2026-08-19T14:22:00+09:00',
    secretUpdatedBy: '류대안',
    state: 'enabled',
    error: null,
    limitUsd: 2,
    archivePath: '/var/lib/tybot/archive/channels/fin',
    createdAt: '2026-08-14T11:30:00+09:00',
    createdBy: '류대안',
  },
  {
    key: 'pilot',
    label: '파일럿',
    role: 'member',
    readable: [],
    botTokenMask: 'xoxb-2094…1d77',
    appTokenMask: 'xapp-6b31…90aa',
    secretUpdatedAt: '2026-08-06T16:10:00+09:00',
    secretUpdatedBy: '류대안',
    state: 'enabled',
    error: null,
    limitUsd: 2,
    archivePath: '/var/lib/tybot/archive/channels/pilot',
    createdAt: '2026-08-05T13:00:00+09:00',
    createdBy: '류대안',
  },
  {
    key: 'site-gimhae',
    label: '현장 김해외동(180182)',
    role: 'member',
    readable: [],
    botTokenMask: 'xoxb-5512…8c30',
    appTokenMask: 'xapp-3f18…4b62',
    secretUpdatedAt: '2026-08-15T09:45:00+09:00',
    secretUpdatedBy: '박정호',
    state: 'enabled',
    error: null,
    limitUsd: 2,
    archivePath: '/var/lib/tybot/archive/channels/site-gimhae',
    createdAt: '2026-08-15T09:40:00+09:00',
    createdBy: '류대안',
  },
  {
    key: 'safety',
    label: '안전관리팀',
    role: 'member',
    readable: [],
    botTokenMask: 'xoxb-8830…5a19',
    appTokenMask: 'xapp-0d54…6c88',
    secretUpdatedAt: '2026-08-21T08:30:00+09:00',
    secretUpdatedBy: '류대안',
    state: 'error',
    error: 'Slack 인증 실패 (invalid_auth) — 봇 토큰이 폐기됐거나 앱이 삭제됐다',
    limitUsd: 2,
    archivePath: '/var/lib/tybot/archive/channels/safety',
    createdAt: '2026-08-21T08:28:00+09:00',
    createdBy: '류대안',
  },
]

/** 콘솔 로그인 사용자.
 *
 * 승인 권한은 관리자(admin)에게만 있습니다. 개발자는 요청만 올립니다.
 * `member` 쪽은 실제 운영에서 현업이 보게 되는 화면을 확인하려고 함께 넣어 뒀습니다 —
 * 레일 아래 '보기 전환' 으로 두 화면을 비교할 수 있습니다.
 */
export const users: Record<ConsoleRole, ConsoleUser> = {
  admin: {
    name: '류대안',
    email: 'dan@taeyoung.com',
    role: 'admin',
    workspaces: ['mgmt', 'fin', 'pilot', 'site-gimhae', 'safety'],
  },
  developer: {
    name: '김수현',
    email: 'sh.kim@taeyoung.com',
    role: 'developer',
    workspaces: ['fin'],
  },
  guest: {
    name: '조회 사용자',
    email: 'guest@taeyoung.com',
    role: 'guest',
    workspaces: ['fin'],
  },
}
