# 마스터·전문 봇 오케스트레이션 Codex 검증

_검증 대상: `4ffce04`, `b052705`_
_검증일: 2026-09-14_
_결론: 아래 발견 사항 코드 보완 완료. Linux 런타임 및 운영 Slack 검증 대기._

## 수정 및 재검증 결과

아래 발견 사항은 최초 검토 기록이며, 2026-09-14 다음과 같이 보완했다.

- 신뢰도 0과 누락을 낮은 신뢰도로 처리해 전문 봇 호출을 막는다.
- 검색 도구와 Hermes 지침을 통일해 예산 내 동의어 재검색을 허용한다.
- decision ID, task index, capability, planner model과 각 업무 결과를 QA 및 콘솔에 연결한다.
- 실제 탐색 근거가 없는 tools 답변은 `evidence_insufficient`로 분리한다.
- 전문 봇 계층이 없을 때 마스터 업무 답변을 기본 차단한다. 기존 비교 측정은 명시적 옵션으로만 허용한다.
- 지원하지 않는 HTTP 실행 모드를 명시적으로 거절하고 Hermes 계약 버전을 v4로 맞춘다.
- `console_schema.sql`에 추적 컬럼과 상태 제약의 멱등 마이그레이션을 추가했다. 배포 시 반드시 적용한다.

보완 후 Python 검증은 Windows에서 실행 불가능한 specialist runtime 셸 테스트 파일
2개를 제외하고 1809 passed였다. 이후 추가한 Slack 복합 질문 추적 회귀 테스트를 포함한
해당 파일은 14 passed였다. 최초 전체 실행 결과와 구분한다.
최종 `ruff check src tests scripts`, 프론트엔드 타입 검사와 빌드도 통과했다.
운영 DB 마이그레이션과 실 Slack 답변 품질은 이 로컬 테스트로 검증하지 않았다.

## 확인된 문제

### P1. 신뢰도 `0`이 낮은 신뢰도가 아니라 통과 값으로 처리된다

`intent.plan()`이 실패하거나 응답을 파싱하지 못하면 규칙 기반 Intent로 폴백하며,
이 Intent의 `routing_confidence`는 기본값 `0.0`이다. 그러나
`specialist_router.serve()`는 `if confidence and confidence < min_confidence`로
검사하므로 `0.0`은 clarification을 건너뛴다. 이어서 전문 봇 호출에는
`confidence or chosen.min_confidence`가 전달되어, 알 수 없는 신뢰도가 최소 통과
신뢰도로 바뀐다.

영향:

- planner 장애·깨진 JSON·필드 누락 때 불확실한 분류가 정상 전문 봇 판정처럼 실행된다.
- 설계의 "planner 실패는 clarification/unavailable", "문턱 미달은 실행하지 않음"을
  위반한다.
- 기존 테스트는 미리 만든 `CLARIFY` outcome만 AnswerEngine에 주므로 실제
  `serve(confidence=0)` 조건을 검사하지 않는다.

필수 수정·검증:

- 신뢰도 누락/0을 명시적인 unknown으로 처리하고 전문 봇을 호출하지 않는다.
- planner 호출 실패, 파싱 실패, confidence 누락, `0`, 문턱 바로 아래를 각각 실제
  `plan -> from_intents -> serve` 경로로 검사한다.

### P1. Hermes 검색 규칙이 서로 반대다

`subbots/hermes/contract/prompt.md`는 낱말을 바꿔 재검색하는 것이 필요하다고
지시한다. 반면 `specialist_tools.SEARCH_DESCRIPTION`과 검색 0건 결과는
"낱말을 바꿔 다시 부르지 말라"고 지시한다.

영향:

- 이번 변경의 핵심 사례인 `미수금`과 `미회수` 같은 표현 차이를 Hermes가 다시
  찾지 못할 수 있다.
- 호출 예산을 코드로 제한했는데도 예전의 재검색 금지 정책이 모델 입력에 남아 있다.

필수 수정·검증:

- 도구 설명과 0건 결과를 검색 예산 정책에 맞게 통일한다.
- 첫 검색 0건 후 동의어·문서명·채널 읽기로 근거를 찾는 도구 호출 시퀀스를
  가짜 응답이 아닌 ToolSpecialist 전체 루프로 검사한다.

### P1. 업무 task별 추적이 보존되지 않는다

한 메시지를 최대 3개 task로 처리하지만 `pilot.py`는 최종 `last` Answer 하나만 QA
레코드에 저장한다. `specialist_call`에는 `qa_record_id`만 있고 `decision_id`, task
kind, required capability가 없다. 또한 `specialist_hook()`이 `serve()`에
`decision_id`를 전달하지 않는다.

초기 검색 0건에서 전문 봇이 성공하는 경로는 Answer에 `required_capability`와
`attempted_specialists`를 넣지 않고, `_unavailable()`도 required capability를
보존하지 않는다. 따라서 콘솔에서 "마스터 판정 -> 전문 봇 시도 -> 응답"을 task별로
재구성할 수 없다.

필수 수정·검증:

- task ID 또는 task ordinal과 decision ID를 전문 봇 호출 기록에 저장한다.
- 복합 질문의 각 task 결과를 별도로 추적하거나 명시적인 자식 레코드로 저장한다.
- 성공, 빈 seed 성공, clarification, 검색 예산 소진, 전문 봇 전체 실패에서
  capability·attempted·error·final responder가 모두 남는지 검사한다.

### P2. `evidence_insufficient` 상태가 선언만 되어 있다

`EVIDENCE_INSUFFICIENT` 상수는 실제 반환 경로에서 사용되지 않는다. 전문 봇이 근거를
못 찾은 경우와 빈 출력·어댑터 실패가 `no_hits` 또는 `unavailable`로 섞인다. 설계에서
요구한 "자료가 없음과 봇 장애 분리"가 완료되지 않았다.

### P2. 전문 봇 계층이 빠지면 마스터가 여전히 업무 답변을 작성한다

운영 `build_bots()`는 specialist hook을 주입하므로 기본 서비스 경로는 막혔다. 하지만
`AnswerEngine.from_env()`와 specialist 없이 직접 생성하는 경로는 경고만 남기고 기존
마스터 업무 답변을 실행한다. 정책을 불변식으로 강제하려면 운영 여부와 무관하게
fail-closed하거나 명시적인 테스트 전용 옵션으로 격리해야 한다.

### P2. `execution_mode=http`가 prompt 모드로 조용히 실행된다

`specialist_adapters.build()`는 `tools`만 분기하고 그 밖의 값은 모두
`PromptSpecialist`로 만든다. 현재 Hermes는 tools 모드라 직접 영향은 없지만, HTTP
전문 봇을 등록하면 선언과 다른 코드가 실행된다. 미구현 모드는 명시적으로 거부해야
묵시적 강등이 발생하지 않는다.

### P3. 계약 버전 표기가 일치하지 않는다

Hermes 프롬프트 front matter는 `version: 4`인데 `tybot-specialist.toml`은
`version = "2"`, `contract_version = "v1"`이다. 서로 다른 버전 축이라면 이름과
콘솔 표시를 분리하고, 같은 계약 버전이라면 하나로 맞춰야 한다.

## 통과한 검증

- 오케스트레이션·후속 질문·전문 봇 집중 테스트: 127 passed
- 콘솔 API 테스트: 108 passed
- `ruff check`: 통과
- 콘솔 `npm run build`: 통과
- 전체 pytest: 1855 passed, 30 failed

전체 테스트의 30개 실패는 Windows의 `bash.exe`가 WSL 배포 스크립트를 실행하지 못해
발생했으며 모두 specialist runtime 셸 테스트에 집중됐다. Linux 지원 환경에서는 별도
재실행이 필요하다. 검증 도중 나타난 미커밋 스키마·배포 변경은 다른 작업으로 보고
이 검토에서 수정하거나 되돌리지 않았다.

## 운영 전 필수 확인

1. DB의 Hermes rules가 파일 계약보다 우선하므로 운영 DB 규칙이 구버전인지 확인한다.
2. 실제 Slack 스레드에서 미수금 후속 질문과 주간 보고 표현 변형을 재현한다.
3. 전문 봇 로그와 QA 기록이 같은 decision/task로 연결되는지 확인한다.
4. Linux에서 전체 pytest와 specialist runtime 셸 테스트를 통과시킨다.
