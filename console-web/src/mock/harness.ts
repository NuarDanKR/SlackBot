/** 봇 하네싱 파일 목데이터.
 *
 * TYBot 은 두 층으로 움직입니다.
 *   - 파이썬 코드: 수집·검색·권한 판정 같은 **통제 로직**
 *   - MD 파일:     답변 규칙·업무 흐름·용어 사전 같은 **하네싱**
 * 현업이 손대는 쪽은 대부분 MD 입니다. 그래서 콘솔에서 편집하고, 어드민이 승인합니다.
 */
import type { HarnessFile, HarnessRequest } from '../types'

const RULES_FIN = `---
workspace: fin
kind: rules
updated: 2026-08-19
---

# 자금팀 봇 답변 규칙

## 반드시 지킬 것
1. 금액은 **아카이브 원문에 적힌 표기 그대로** 옮긴다. 억/천만 단위를 임의로 환산하지 않는다.
2. 기성금·선급금·유보금은 서로 다른 항목이다. 원문에서 구분되지 않으면 구분해서 답하지 않는다.
3. 자금 집행 여부를 묻는 질문에는 **결재 완료 여부와 실제 지급 여부를 나눠서** 답한다.
4. 원문에 없는 금액은 추정하지 않는다. "아카이브에서 근거를 찾지 못했습니다"로 답한다.

## 답변 형식
- 결론 한 줄 → 근거 항목 → 출처
- 금액은 원문 표기 뒤에 괄호로 원문 날짜를 붙인다. 예: 3억 2천만원(08-12)

## 자주 묻는 항목
| 질문 유형 | 우선 확인할 채널 |
|---|---|
| 기성 청구 | #팀_자금(ABB540)_주간보고 |
| 지급 일정 | #팀_자금(ABB540)_집행 |
| 예산 잔액 | #팀_자금(ABB540)_예산 |
`

const WORKFLOW_FIN = `---
workspace: fin
kind: workflow
updated: 2026-08-17
---

# 주간보고 취합 흐름

## 매주 금요일 16:00
1. 최근 7일 원문을 채널별로 훑는다.
2. 결정된 것 / 진행 중 / 대기 로 나눈다.
3. 금액이 등장한 항목은 **원문 라인을 그대로 인용**한다.
4. 판단이 서지 않는 항목은 비워 두고 "원문에서 확인되지 않음"으로 남긴다.

## 하지 않을 것
- 진척률을 숫자로 만들어내지 않는다.
- 지난주 보고서를 근거로 삼지 않는다. 근거는 항상 원문이다.
`

const RULES_SITE = `---
workspace: site-gimhae
kind: rules
updated: 2026-08-15
---

# 현장 김해외동(180182) 봇 답변 규칙

## 현장 용어
- "타설"은 콘크리트 타설을 뜻한다. 일정 문의는 공정표 채널을 먼저 본다.
- "검측"은 감리 검측을 뜻한다. 통과 여부는 원문에 명시된 것만 답한다.

## 안전 관련
안전 사고·재해 관련 질문은 **요약하지 않는다.** 원문 라인을 그대로 보여주고
담당자 확인을 안내한다. 요약 과정에서 사실이 뭉개지면 안 되는 영역이다.

## 사진·도면 첨부
현재 이미지와 도면은 변환하지 않는다. 목록만 남으므로,
"사진에서 확인해 달라"는 요청에는 파일명을 알려주고 직접 열어보게 안내한다.
`

const GLOSSARY = `---
workspace: mgmt
kind: glossary
updated: 2026-08-12
---

# 사내 용어 사전

봇이 질문의 핵심어를 뽑을 때 참고합니다. 같은 뜻의 다른 표현을 묶어 둡니다.

| 표준 용어 | 같이 쓰는 표현 |
|---|---|
| 기성금 | 기성, 기성청구, 진행급 |
| 선급금 | 선금, 착수금 |
| 준공 | 완공, 마감 |
| 결재 | 결제(오기), 승인요청 |

## 주의
"결제"와 "결재"는 다릅니다. 원문에 "결제"로 적혀 있어도 문맥이 승인 절차면
검색어에 둘 다 넣습니다. 다만 **답변에서는 원문 표기를 그대로** 씁니다.
`

export const harnessFiles: HarnessFile[] = [
  {
    workspace: 'fin',
    workspaceLabel: '자금팀',
    path: 'harness/fin/rules.md',
    title: '답변 규칙',
    kind: 'rules',
    updatedAt: '2026-08-19T14:22:00+09:00',
    updatedBy: '류대안',
    pendingRequestId: 'hr-42',
    content: RULES_FIN,
  },
  {
    workspace: 'fin',
    workspaceLabel: '자금팀',
    path: 'harness/fin/workflow.md',
    title: '주간보고 취합 흐름',
    kind: 'workflow',
    updatedAt: '2026-08-17T10:05:00+09:00',
    updatedBy: '김수현',
    pendingRequestId: null,
    content: WORKFLOW_FIN,
  },
  {
    workspace: 'site-gimhae',
    workspaceLabel: '현장 김해외동(180182)',
    path: 'harness/site-gimhae/rules.md',
    title: '답변 규칙',
    kind: 'rules',
    updatedAt: '2026-08-15T09:40:00+09:00',
    updatedBy: '박정호',
    pendingRequestId: null,
    content: RULES_SITE,
  },
  {
    workspace: 'mgmt',
    workspaceLabel: '경영본부',
    path: 'harness/mgmt/glossary.md',
    title: '사내 용어 사전',
    kind: 'glossary',
    updatedAt: '2026-08-12T16:30:00+09:00',
    updatedBy: '류대안',
    pendingRequestId: null,
    content: GLOSSARY,
  },
]

const AFTER_FIN = RULES_FIN.replace(
  '4. 원문에 없는 금액은 추정하지 않는다. "아카이브에서 근거를 찾지 못했습니다"로 답한다.',
  `4. 원문에 없는 금액은 추정하지 않는다. "아카이브에서 근거를 찾지 못했습니다"로 답한다.
5. **부가세 포함/별도가 원문에 명시되지 않으면 어느 쪽인지 단정하지 않는다.**
   금액을 옮긴 뒤 "원문에 부가세 표기 없음"을 덧붙인다.`,
)

export const harnessRequests: HarnessRequest[] = [
  {
    id: 'hr-42',
    workspace: 'fin',
    workspaceLabel: '자금팀',
    path: 'harness/fin/rules.md',
    title: '답변 규칙',
    requester: '김수현',
    requestedAt: '2026-08-21T11:48:00+09:00',
    reason:
      '봇이 기성금을 부가세 포함 금액처럼 답한 사례가 있었습니다. 원문에 표기가 없으면 단정하지 않도록 규칙을 한 줄 추가했습니다.',
    added: 3,
    removed: 0,
    checks: [
      { id: 'schema', label: '문서 형식', state: 'pass', detail: '프론트매터·제목 구조 정상' },
      { id: 'secrets', label: '시크릿 유출', state: 'pass', detail: '토큰·키 패턴 없음' },
      { id: 'ruff', label: '금지 표현', state: 'pass', detail: '개인정보·PII 항목 없음' },
      { id: 'pytest', label: '규칙 충돌', state: 'pass', detail: '기존 4개 규칙과 충돌 없음' },
    ],
    state: 'awaiting_approval',
    before: RULES_FIN,
    after: AFTER_FIN,
  },
]
