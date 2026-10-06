/**
 * 실행 모드 — Hermes 가 **누구 밑에서 도는가**.
 *
 *   HERMES_MODE=tybot   TYBot 연동. 원문 쓰기를 전부 막는다
 *   (미설정 또는 그 외)  PF 직접 실행. 지금까지와 똑같다
 *
 * ## 왜 필요한가
 *
 * TYBot 연동에서 Hermes 는 **자기 권한이 없다.** 근거는 TYBot 의 권한 검사된
 * 도구(search·read_channel·read_document·fetch_recent_slack)로만 읽고, 권한은
 * `RequestContext` 한 곳에서 판정된다(TYBot 원칙 3). 그 구조에서 Hermes 가 자기
 * 로컬 아카이브에 원문을 쓰면 **판정을 거치지 않은 사본**이 하나 더 생긴다.
 *
 * 전에는 「TYBot 이 Hermes 소스를 실행하지 않으니 안전하다」 였다. 그건 사실이지만
 * 보장이 아니다 — `npm run ingest` 한 줄이면 돌고, `.claude/skills/` 아래 스크립트는
 * 사람이 명령을 치지 않아도 **에이전트가 집어서** 돌릴 수 있다. 「아무도 안 부른다」
 * 와 「부를 수 없다」 는 다르다. 이 파일이 후자를 만든다.
 *
 * ## 기본값이 PF 인 이유
 *
 * 켜는 쪽을 기본값으로 두면, 환경변수를 안 넘긴 PF 운영이 어느 날 조용히 멈춘다.
 * **막는 쪽이 기본값** 이라는 TYBot 원칙 3 과 반대로 보이지만 대상이 다르다 —
 * 원칙 3 은 「자료 열람」 의 기본값이고, 여기는 「이미 돌고 있는 운영」 의 기본값이다.
 * 운영을 끄는 결정은 명시적이어야 한다.
 *
 * 이 모듈은 **판정만 한다.** 무엇을 막을지는 부르는 쪽이 정하고, 막는 자리마다
 * 회귀 시험이 붙어 있다(`scripts/check-tybot-mode.js`).
 */

export const TYBOT = 'tybot';
export const PF = 'pf';

/** 지금 모드. 모르는 값은 PF 로 본다 — 오타가 운영을 멈추게 하지 않는다. */
export function mode() {
  const raw = String(process.env.HERMES_MODE || '').trim().toLowerCase();
  return raw === TYBOT ? TYBOT : PF;
}

export function isTybotMode() {
  return mode() === TYBOT;
}

/** 막힌 동작. 코드로 구별할 수 있어야 호출부가 「실패」 와 「금지」 를 가른다. */
export class ArchiveWriteBlocked extends Error {
  constructor(action) {
    super(
      `[TYBot 연동 모드] ${action} 은(는) 막혀 있습니다.\n` +
        '연동 모드에서 Hermes 는 원문을 쓰지 않습니다 — 근거는 TYBot 의 권한 검사된 ' +
        '도구로만 읽습니다.\n' +
        'PF 직접 실행이라면 HERMES_MODE 를 비우고 다시 실행하세요.'
    );
    this.name = 'ArchiveWriteBlocked';
    this.code = 'tybot_mode_write_blocked';
    this.action = action;
  }
}

/**
 * 원문을 쓰려는 자리마다 **맨 앞에서** 부른다.
 *
 * 되돌려 주는 값이 없다 — 통과하거나 던지거나 둘뿐이다. 불리언을 돌려주면
 * 부르는 쪽이 확인을 잊어도 조용히 지나간다.
 */
export function assertMayWriteArchive(action) {
  if (isTybotMode()) {
    throw new ArchiveWriteBlocked(action);
  }
}

/* ── 실행 역할 ────────────────────────────────────────────────────────────
 *
 * 모드가 「누구 밑에서 도는가」 라면, 역할은 「무엇을 내가 맡는가」 다. 둘을 같은
 * 값으로 묶으면 다음에 역할 하나만 옮길 때 모드 전체를 흔들어야 한다.
 *
 * ## 연동에서 빠지는 것은 「요약」 이 아니라 「직접 게시」 다
 *
 * 흔한 오해라 이름부터 그렇게 지었다(`digest-publish`). 연동 모드에서도 **요약을
 * 만드는 규칙은 Hermes 것**이다 — TYBot 은 `contract/summary-review.md` 를 런타임에
 * 읽어 그 규칙으로 후보를 만들고, 사람 확인은 자기 DM·Canvas 로 받는다. 즉 TYBot 은
 * **승인 인터페이스**이고, 요약과 재요약의 주인은 Hermes 다.
 *
 * 빠지는 것은 Hermes 가 **자기 스케줄로 Slack 에 직접 올리는 경로** 하나다. 그게
 * 남아 있으면 사람은 같은 날 요약을 두 번 받고 — 하나는 승인을 거쳤고 하나는 안
 * 거쳤는데 — 둘의 숫자가 다르면 어느 쪽이 맞는지 알 방법이 없다.
 *
 * 구조는 `archive-run` 과 같다. 상황판은 **무엇이 남았나·순서·마감**만 알고 절차는
 * 하위 스킬이 쥔다. 여기서는 TYBot 이 상황판이고 Hermes 가 절차를 쥔다. 절차를
 * 상황판에 복사하지 않는 이유도 같다 — 복사하면 원본이 바뀔 때 **에러 없이 낡는다.**
 *
 * ## 수집은 어느 쪽도 아니다
 *
 * 2026-09-23 분리 합의로 원문 수집·첨부 변환·provenance 는 **Archiving Bot** 이
 * 맡는다(`docs/design/archiving-bot-separation-2026-09-23.md`). TYBot Master 도
 * 수집에서 빠지는 중이다. 여기서 `ingest` 를 끄는 것은 그 이관의 한 걸음이고,
 * 이관이 끝나면 이 역할 자체가 사라져야 한다.
 */
export const ROLES = {
  /** 원문 수집·소급·첨부 저장. **목표 소유자는 Archiving Bot** — 이관되면 사라진다 */
  INGEST: 'ingest',
  /** 일일·주간 요약을 **Slack 에 직접 올리는 것**. 요약 생성 자체가 아니다 */
  DIGEST_PUBLISH: 'digest-publish',
  /** 위생 점검 발송. 연동 모드의 점검 대상은 Archiving Bot 이 쌓는 자료다 */
  HEALTH: 'health',
  /** 질문 응답(슬랙 질문·DM). 어느 모드에서도 Hermes 가 맡는다 */
  ANSWER: 'answer',
};

/** PF 직접 실행이 맡는 역할 — 지금까지와 같다. */
const PF_ROLES = new Set([ROLES.INGEST, ROLES.DIGEST_PUBLISH, ROLES.HEALTH, ROLES.ANSWER]);

/**
 * TYBot 연동이 Hermes 에게 남기는 역할.
 *
 * `answer` 하나지만 **요약을 안 한다는 뜻이 아니다.** 요약은 TYBot 이 전문 봇으로
 * 부를 때 Hermes 의 규칙으로 만들어진다 — 그건 스케줄이 아니라 호출이라 여기 없다.
 */
const TYBOT_ROLES = new Set([ROLES.ANSWER]);

export function rolesFor(current = mode()) {
  return current === TYBOT ? TYBOT_ROLES : PF_ROLES;
}

/** 이 역할을 지금 내가 맡는가. 스케줄 등록·발송 직전에 묻는다. */
export function ownsRole(role) {
  return rolesFor().has(role);
}

/** 역할이 없는데 하려 할 때. 쓰기 금지(`ArchiveWriteBlocked`)와 **다른 사유**다. */
export class RoleNotOwned extends Error {
  constructor(role, action) {
    super(
      `[TYBot 연동 모드] ${action} 은(는) 이 프로세스의 역할이 아닙니다(${role}).\n` +
        '연동 모드에서 요약·위생 점검 발송은 TYBot 이 맡습니다 — 여기서 또 보내면 ' +
        '같은 날 같은 내용이 두 번 나갑니다.\n' +
        'PF 직접 실행이라면 HERMES_MODE 를 비우고 다시 실행하세요.'
    );
    this.name = 'RoleNotOwned';
    this.code = 'tybot_mode_role_not_owned';
    this.role = role;
    this.action = action;
  }
}

/** 발송 직전에 부른다. 통과하거나 던지거나 둘뿐이다. */
export function assertOwnsRole(role, action) {
  if (!ownsRole(role)) {
    throw new RoleNotOwned(role, action);
  }
}
