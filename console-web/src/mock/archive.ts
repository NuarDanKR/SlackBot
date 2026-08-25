/** 수집된 아카이브 문서 목데이터.
 *
 * 실제 경로는 봇 서버의 `ARCHIVE_DIR` 아래입니다 —
 * `/var/lib/tybot/archive/channels/<워크스페이스>/<채널>.md`
 *
 * `content` 는 어드민에게만 내려오는 값입니다(`types.ts` 상단 설명 참조).
 * 목데이터에서는 값을 담아 두고, 화면에서 역할에 따라 가립니다.
 */
import type { CollectedDoc } from '../types'

const DOC_FIN_WEEKLY = `---
workspace: fin
channel: "#팀_자금(ABB540)_주간보고"
visibility: private
acl: [#팀_자금(ABB540)_주간보고]
share_with: [mgmt]
doc_count: 214
last_ingested: 2026-08-21T13:58+09:00
---

## 요약 (사람이 관리, 봇은 수정 금지)
- 8월 3주: 김해외동 기성 3차 청구 접수, 결재 진행 중

## 원문 (자동 취합, 편집 금지)
> [2026-08-19 09:12] 김수현: 김해외동 3차 기성 청구서 접수했습니다. 금액은 3억 2천만원입니다
> [2026-08-19 09:20] 이순신: 결재 올려주세요. 지난 2차 때 첨부 누락 있었으니 확인 부탁합니다
> [2026-08-19 09:31] 김수현: [첨부:변환] 기성내역_3차.xlsx (xlsx, 84KB)
> [2026-08-19 09:31] 김수현: [첨부추출:기성내역_3차.xlsx] 시트: 기성내역
> [2026-08-19 09:31] 김수현: [첨부추출:기성내역_3차.xlsx] 공종 | 계약금액 | 기성률 | 금액
> [2026-08-19 09:31] 김수현: [첨부추출:기성내역_3차.xlsx] 토공 | 1,240,000,000 | 62% | 768,800,000
> [2026-08-19 09:31] 김수현: [첨부추출:기성내역_3차.xlsx] 골조 | 2,180,000,000 | 41% | 893,800,000
> [2026-08-19 14:02] 박정호: 현장 확인 완료했습니다. 기성률은 내역서와 일치합니다
> [2026-08-20 10:15] 이순신: 결재 승인 났습니다. 지급 예정일은 8월 28일입니다
> [2026-08-21 13:58] 김수현: 지급 요청 등록했습니다. 세금계산서는 26일에 받기로 했습니다
`

const DOC_FIN_BUDGET = `---
workspace: fin
channel: "#팀_자금(ABB540)_예산"
visibility: private
acl: [#팀_자금(ABB540)_예산]
share_with: []
doc_count: 96
last_ingested: 2026-08-20T17:41+09:00
---

## 요약 (사람이 관리, 봇은 수정 금지)
-

## 원문 (자동 취합, 편집 금지)
> [2026-08-18 11:20] 이순신: 3분기 예산 잔액 확인했습니다. 집행률 58%입니다
> [2026-08-20 17:41] 김수현: 4분기 예산 요청안 초안 공유합니다. 다음 주 회의에서 확정하겠습니다
`

const DOC_SITE = `---
workspace: site-gimhae
channel: "#현장_김해외동(180182)_채팅방"
visibility: private
acl: [#현장_김해외동(180182)_채팅방]
share_with: []
doc_count: 431
last_ingested: 2026-08-18T17:12+09:00
---

## 요약 (사람이 관리, 봇은 수정 금지)
- 3층 골조 타설 완료, 4층 준비 중

## 원문 (자동 취합, 편집 금지)
> [2026-08-18 08:05] 박정호: 오늘 3층 슬래브 타설 시작합니다. 레미콘 6대 예정입니다
> [2026-08-18 11:30] 최민석: 검측 통과했습니다. 감리 확인 서명 받았습니다
> [2026-08-18 15:44] 박정호: 타설 완료했습니다. 양생 3일 예정이라 4층 먹매김은 22일부터입니다
> [2026-08-18 17:12] 최민석: [첨부:미변환] 3층타설_현황.jpg (jpg, 2104KB)
`

const DOC_BROKEN = `workspace: safety
channel: "#팀_안전(SAF100)_일일점검"

## 원문
> [2026-08-21 08:30] 정한길: 오늘 일일점검 시작합니다
`

export const collectedDocs: CollectedDoc[] = [
  {
    workspace: 'fin',
    workspaceLabel: '자금팀',
    channel: '#팀_자금(ABB540)_주간보고',
    path: 'channels/fin/팀_자금(ABB540)_주간보고.md',
    lines: 214,
    bytes: 48_210,
    lastIngestedAt: '2026-08-21T13:58:00+09:00',
    visibility: 'private',
    acl: ['#팀_자금(ABB540)_주간보고'],
    shareWith: ['mgmt'],
    schemaError: null,
    attachmentLines: 31,
    content: DOC_FIN_WEEKLY,
  },
  {
    workspace: 'fin',
    workspaceLabel: '자금팀',
    channel: '#팀_자금(ABB540)_예산',
    path: 'channels/fin/팀_자금(ABB540)_예산.md',
    lines: 96,
    bytes: 19_440,
    lastIngestedAt: '2026-08-20T17:41:00+09:00',
    visibility: 'private',
    acl: ['#팀_자금(ABB540)_예산'],
    shareWith: [],
    schemaError: null,
    attachmentLines: 0,
    content: DOC_FIN_BUDGET,
  },
  {
    workspace: 'site-gimhae',
    workspaceLabel: '현장 김해외동(180182)',
    channel: '#현장_김해외동(180182)_채팅방',
    path: 'channels/site-gimhae/현장_김해외동(180182)_채팅방.md',
    lines: 431,
    bytes: 92_800,
    lastIngestedAt: '2026-08-18T17:12:00+09:00',
    visibility: 'private',
    acl: ['#현장_김해외동(180182)_채팅방'],
    shareWith: [],
    schemaError: null,
    attachmentLines: 64,
    content: DOC_SITE,
  },
  {
    workspace: 'safety',
    workspaceLabel: '안전관리팀',
    channel: '#팀_안전(SAF100)_일일점검',
    path: 'channels/safety/팀_안전(SAF100)_일일점검.md',
    lines: 1,
    bytes: 210,
    lastIngestedAt: '2026-08-21T08:30:00+09:00',
    visibility: 'private',
    acl: [],
    shareWith: [],
    schemaError: '프론트매터(--- ... ---)가 없습니다 — 이 문서는 답변 근거로 쓰이지 않습니다',
    attachmentLines: 0,
    content: DOC_BROKEN,
  },
]

/** 원문 열람 기록. 실제로는 서버의 감사 로그에 남고 콘솔은 읽기만 합니다. */
export const readAudit = [
  { at: '2026-08-21T13:12:00+09:00', actor: '류대안', path: 'channels/fin/팀_자금(ABB540)_주간보고.md' },
  { at: '2026-08-20T09:41:00+09:00', actor: '류대안', path: 'channels/site-gimhae/현장_김해외동(180182)_채팅방.md' },
]
