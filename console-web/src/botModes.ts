// 수집 모드와 연결 상태를 **사람 말로** 옮긴다.
//
// 결정: 2026-09-29 오너 지시.
//
// ## 왜 한 곳에 모으나
//
// 예전 화면은 `off`·`shadow`·`active` 를 그대로 보여 줬다. 그 단어는 우리가 코드에서
// 쓰는 이름이고, 운영자에게는 **무엇이 달라지는지 말해 주지 않는다.** `off` 를 본
// 사람은 "수집이 꺼졌다"로 읽지만 실제로는 "Archiver 가 관여하지 않고 Master 가
// 그대로 쓴다"다 — 원문은 계속 쌓인다.
//
// 뜻을 먼저 보이고, 내부 키는 **상세·툴팁에서만** 보여 준다. 키를 주 화면에
// 나란히 붙이면 결국 사람이 키를 읽고, 뜻은 장식이 된다.
//
// 한 곳에 모으는 이유는 화면이 여럿이기 때문이다. 목록·상세·확인 모달이 각자
// 문구를 쓰면 같은 상태가 화면마다 다르게 불리고, 그때 사람은 둘이 다른 것인 줄 안다.

export type ChannelMode = 'off' | 'shadow' | 'active' | 'paused'
export type ConnectionState = 'draft' | 'disabled' | 'enabled' | 'error' | 'retired'
export type RouteMode = 'disabled' | 'shadow' | 'active'

export type Meaning = {
  /** 주 화면에 보이는 말. 내부 키를 넣지 않는다. */
  label: string
  /** 한 줄 더. 무엇이 달라지는지. */
  detail: string
  /** 상세·툴팁에서만 보이는 내부 키. */
  key: string
  tone: 'neutral' | 'good' | 'warn' | 'bad'
}

export const CHANNEL_MODES: Record<ChannelMode, Meaning> = {
  off: {
    label: 'Archiver 미적용 · Master 유지',
    detail: 'Archiver 는 이 채널에 관여하지 않습니다. 원문은 Master 가 그대로 씁니다.',
    key: 'off',
    tone: 'neutral',
  },
  shadow: {
    label: '그림자 대조 · 운영본은 Master',
    detail:
      'Archiver 가 같은 대화를 따로 모아 Master 결과와 대조합니다. '
      + '답변이 쓰는 원문은 여전히 Master 것입니다.',
    key: 'shadow',
    tone: 'warn',
  },
  active: {
    label: 'Archiver 운영 수집',
    detail: '이 채널의 운영 원문을 Archiver 가 씁니다. 전환 시점(watermark)이 기록됩니다.',
    key: 'active',
    tone: 'good',
  },
  paused: {
    label: '일시 중지 · 양쪽 쓰기 중단',
    detail:
      'Master 도 Archiver 도 이 채널 원문을 쓰지 않습니다. '
      + '멈춘 동안의 대화는 나중에 백필해야 합니다.',
    key: 'paused',
    tone: 'bad',
  },
}

export const CONNECTION_STATES: Record<ConnectionState, Meaning> = {
  draft: {
    label: '토큰 미등록',
    detail: '아직 봇/앱 토큰 쌍이 없습니다. 넣어도 바로 켜지지 않습니다.',
    key: 'draft',
    tone: 'neutral',
  },
  disabled: {
    label: '연결 꺼짐',
    detail: '토큰은 있으나 이 앱으로 Slack 이벤트를 받지 않습니다.',
    key: 'disabled',
    tone: 'neutral',
  },
  enabled: {
    label: 'Slack 연결 사용',
    detail: '토큰 등록 때 확인된 Team·Bot 사용자로 이 워크스페이스에 붙어 있습니다.',
    key: 'enabled',
    tone: 'good',
  },
  error: {
    label: '연결 오류',
    detail: '런타임이 오류를 보고했습니다. 상세의 오류 문구를 보세요.',
    key: 'error',
    tone: 'bad',
  },
  retired: {
    label: '사용 종료',
    detail: '더 쓰지 않는 연결입니다. 기록은 남습니다.',
    key: 'retired',
    tone: 'neutral',
  },
}

export const ROUTE_MODES: Record<RouteMode, Meaning> = {
  disabled: {
    label: '내부 호출 안 함',
    detail: 'Master 가 이 봇을 부르지 않습니다. 질문은 Master 가 직접 답합니다.',
    key: 'disabled',
    tone: 'neutral',
  },
  shadow: {
    label: '그림자 호출 · 사용자에게 전달 안 함',
    detail: '같은 질문을 이 봇에게도 보내 결과만 기록합니다. 답변에는 쓰이지 않습니다.',
    key: 'shadow',
    tone: 'warn',
  },
  active: {
    label: '실제 호출',
    detail: 'Master 가 이 봇의 답을 사용자에게 전달합니다.',
    key: 'active',
    tone: 'good',
  },
}

/** 모르는 값도 화면에 나와야 한다. 조용히 빈칸이 되면 상태가 없는 것처럼 보인다. */
export function meaningOf<T extends string>(
  table: Record<string, Meaning>,
  value: T | string | null | undefined,
): Meaning {
  const found = value ? table[value] : undefined
  if (found) return found
  return {
    label: '알 수 없는 상태',
    detail: '콘솔이 모르는 값입니다. 스키마와 화면 버전이 어긋났을 수 있습니다.',
    key: String(value ?? ''),
    tone: 'bad',
  }
}

/** 상세·툴팁에 쓰는 한 줄. **여기서만** 내부 키를 보여 준다. */
export function tooltipOf(meaning: Meaning): string {
  return `${meaning.detail} (내부 값: ${meaning.key})`
}
