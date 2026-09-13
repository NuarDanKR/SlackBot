# 마스터 판단과 전문 봇 답변 오케스트레이션

> 작성: 2026-09-14  
> 구현 담당: Claude  
> 검증 담당: Codex  
> 상태: 구현 완료 (2026-09-14) · Codex 검증 대기  
> 근거: [`2026-09-13-master-specialist-routing-review.md`](../verification/2026-09-13-master-specialist-routing-review.md)

## 1. 오너 결정

2026-09-14 보완: 구현 순서는 **도구 연결 복구 → 후속 질문 전달 → 권한 내 검색·문서
읽기 → 업무별 출력 조정 → 라우팅 통합**이다. 세부 구현과 비교 검증은
[`hermes-integration-fidelity.md`](hermes-integration-fidelity.md)를 함께 따른다.
다른 부서 Hermes의 실행 기록은 아직 대조하지 않았으며, 품질 차이의 원인을 하나로
확정하지 않는다.

TYBot 마스터와 전문 봇의 책임을 다음과 같이 고정한다.

- **마스터 TYBot은 업무 답변을 작성하지 않는다.** 같은 스레드의 맥락을 해석하고,
  사용자가 실제로 요청한 작업과 필요한 전문 능력을 판단한다.
- **근거 기반 업무 답변은 전문 봇만 작성한다.** 요약, 사실 조회, 분석, 보고서 작성과
  업무 권고가 이에 해당한다.
- TYBot 코드는 권한, 채널/DM 범위, PII, 근거 추출, 전문 봇 허용 여부, 출력 계약과
  출처 링크를 소유한다. 이 판단을 LLM이나 전문 봇에 위임하지 않는다.
- 전문 봇이 실패해도 마스터 LLM이 원문을 읽고 대신 답하지 않는다. 같은 능력을 가진
  다른 승인 전문 봇을 시도하고, 없으면 전문 답변 불가를 명시한다.
- 상태, 도움말, 명령 실행 결과, 추가 설명 요청처럼 업무 지식을 생성하지 않는 문구는
  TYBot이 결정적으로 응답할 수 있다.

이 문서는 `bot-hierarchy.md`와 BACKLOG B-36/B-42의 기존 “마스터 직접 답변 폴백”
정책을 대체한다. `agent-architecture.md`의 “LLM이 쓰기·권한·스케줄을 자율 결정하지
않는다”는 원칙은 그대로 유지한다. 여기서 마스터 LLM이 담당하는 것은 언어적 맥락과
전문 능력 판정뿐이다.

## 2. 현재 장애의 구조적 원인

현재 한 질문은 다음 두 확률적 판정을 따로 거친다.

```text
intent.plan(): 질문 분해와 의도 판정
  -> AnswerEngine: 근거 검색 또는 이전 근거 복원
  -> specialist_router.route(): 전문 봇을 다시 LLM으로 판정
  -> 전문 봇 실패/미선택: 마스터 LLM이 업무 답변 생성
```

첫 번째 planner는 이전 질문과 주제를 보지만, 두 번째 전문 봇 라우터는 현재 task의
질문 문자열만 본다. `내가 이전에 요청했던 내용을 다시 확인해줘`처럼 지칭어가 남으면
근거는 미수금 문서로 복원돼도 전문 봇 라우터는 그 사실을 모른다. 또한 `none`, 낮은
신뢰도, DB 오류, 라우터 오류와 전문 봇 오류가 모두 마스터 직접 답변으로 합쳐진다.

분류 품질 문제의 핵심은 Python 정규식이 아니다. 현재도 의도 분류와 전문 봇 선택은
각각 LLM을 호출한다. 문제는 **맥락이 다른 두 번의 LLM 판정**과 **모든 실패를 마스터
업무 답변으로 바꾸는 정책**이다.

## 3. 목표 처리 흐름

```text
현재 질문 + 같은 스레드의 구조화된 이전 사용자 질문/주제/근거 좌표
  -> MasterPlanner LLM: 질문 분해 + 지칭 해석 + 필요한 전문 능력 판정 (한 번)
  -> 결정적 검증: 출력 스키마, 쓰기 명령 분리, 채널/DM 범위, 활성 전문 봇
  -> 현재 요청자의 ACL과 PII 정책으로 근거 복원/검색
  -> 전문 봇 선택 확정 및 허용된 텍스트/시각 근거 전달
  -> Hermes가 검색·읽기를 요청하고 TYBot ToolBox가 매 호출의 권한과 PII를 검사
  -> 전문 봇의 업무 답변
  -> TYBot 코드의 출력 계약/근거 ID 검증과 출처 링크 부착
  -> Slack 전송 + QA/전문 봇 호출 추적
```

작업 분해와 라우팅의 의미 판정은 정상 경로에서 한 번만 한다. 전문 봇의 검색·읽기
도구 루프와 허용 모델 재시도까지 한 번으로 제한하는 뜻은 아니다. 두 번째 LLM으로 다시
라우팅하지 않고, 첫 판정의 `required_capability`와 현재 활성 레지스트리로 전문 봇을
확정한다.

## 4. 데이터 계약

### 4.1 `MasterDecision`

`Intent`에 임시 필드를 계속 붙이지 말고 오케스트레이션 결과를 별도 불변 모델로 둔다.
한 사용자 메시지에는 최대 3개의 `MasterTask`가 있을 수 있다.

```python
@dataclass(frozen=True)
class MasterDecision:
    tasks: tuple[MasterTask, ...]
    planner_model: str
    decision_id: str


@dataclass(frozen=True)
class MasterTask:
    kind: Literal[
        "system", "clarify", "factual", "summary", "analysis", "advice", "write"
    ]
    original_fragment: str
    standalone_question: str
    required_capability: str
    suggested_specialist: str = ""
    routing_confidence: float = 0.0
    parent_record_ids: tuple[str, ...] = ()
    topic_terms: tuple[str, ...] = ()
    document_query: tuple[str, ...] = ()
    clarification: str = ""
```

규칙:

- `original_fragment`는 감사와 복합 질문 분리 확인용이며 사용자 원문 조각이다.
- `standalone_question`은 `그 문서`, `이전에 요청한 내용`을 이전 사용자 질문과 주제로
  풀어 쓴 문장이다. 전문 봇에는 이 값을 전달한다.
- `parent_record_ids`는 planner가 임의 생성하지 않는다. 입력으로 제공한 opaque turn ID
  중 선택하게 하고 코드가 허용 목록과 대조한다.
- `required_capability`는 코드가 제공한 열거형만 허용한다. 예:
  `internal_document_qa`, `internal_document_summary`, `legal_analysis`,
  `tax_analysis`, `construction_analysis`.
- `suggested_specialist`도 요청 시점에 활성화된 후보 키만 허용한다. 최종 선택 권한은
  레지스트리를 검증하는 코드에 있다.
- `time_scope`, 날짜 범위, 채널/DM 범위는 기존처럼 결정적 코드가 원문 질문에서 계산한다.
  LLM이 `여태까지`를 임의의 7일로 바꾸거나 다른 채널로 넓히지 못하게 한다.
- LLM이 만든 `topic_terms`와 `document_query`는 검색 힌트일 뿐이다. 근거 권한이나
  source ID가 아니다.

### 4.2 planner 입력

planner에는 다음만 전달한다.

- 현재 사용자 질문
- 같은 스레드의 이전 **사용자 질문**, 주제, 문서 표시명, 처리 종류와 opaque QA ID
- 요청 위치가 채널인지 DM인지
- 사용할 수 있는 전문 능력과 활성 전문 봇의 키·분야·routing hint

이전 봇 답변 본문과 근거 원문은 planner 입력으로 사용하지 않는다. 구형 QA 레코드의
`legacy_answer` 의존도 제거한다. 마이그레이션 기간에는 지칭 해석 후보 생성에만 쓰되,
그 문장을 `standalone_question`이나 근거로 복제하지 않고 사용 로그를 남긴다.

### 4.3 전문 봇 요청

전문 봇에는 다음만 전달한다.

```text
standalone_question
required_capability
request scope/authorization ID
권한과 PII 검사를 통과한 EvidenceRef + 텍스트
허용된 VisualEvidenceRef + 바이트 또는 격리된 읽기 핸들
```

전문 봇은 사용자 권한, 검색 범위, 출처 문구를 결정하지 않는다. 전문 봇이 사용한 모든
source ID는 요청에 포함된 ID 또는 같은 `ToolBox`가 현재 요청 권한으로 반환한 ID여야
한다.

## 5. 전문 봇 선택과 실패 정책

### 5.1 선택

1. `MasterTask.required_capability`를 지원하고 현재 워크스페이스에서 승인·활성 상태인
   전문 봇만 후보로 만든다.
2. `suggested_specialist`가 후보에 있으면 우선 사용한다.
3. 제안이 없거나 유효하지 않으면 capability의 명시적 우선순위로 선택한다.
4. 내부 문서 질의와 요약의 기본 전문 봇은 활성 Hermes다.
5. 비활성, 미승인, health error, 계약 버전 불일치 전문 봇은 후보에 넣지 않는다.

같은 검증된 MasterTask와 같은 레지스트리 스냅샷에는 같은 선택 결과가 나와야 한다.
LLM의 의미 판정 자체가 완전히 결정적이라고 주장하지 않는다. 동일 의미 질문의 판정
일관성은 골든셋으로 측정한다. 선택 확정 단계에서 추가 LLM 호출을 하지 않는다.
분류 신뢰도가 문턱 아래이거나 지칭 대상이 여러 개면 임의의 기본 전문 봇을 선택하지
않고 추가 설명을 요청한다. 후보 키가 위조되었으면 파싱 오류로 처리한다.

### 5.2 실패

```text
선택 전문 봇 성공
  -> 출력 계약 검증 -> 출처 부착

선택 전문 봇 실행 실패/시간 초과/출력 계약 위반
  -> 같은 capability의 다음 승인 전문 봇
  -> 모두 실패하면 deterministic unavailable 응답
```

분류 신뢰도 미달은 실행 장애가 아니다. `clarify`로 처리하고 후보 체인을 돌리지 않는다.
전문 봇이 근거 부족을 보고하면 `evidence_insufficient`로 구분한다. 자료가 없는 문제를
다른 모델이 답하도록 돌려 해결하지 않는다. 검색 예산 소진도 실제 자료 없음과 구분한다.
실행 실패 체인은 후보별 최대 한 번, 요청 전체 시간·비용·호출 예산 내에서만 허용한다.

마지막 응답 예:

```text
요청한 문서 답변을 생성할 수 없습니다.
처리 단계: 전문 봇 호출
사유 코드: specialist-timeout
원문과 첨부는 변경되지 않았습니다.
```

서비스 로그에는 traceback을 남길 수 있지만 Slack과 감사 메타데이터에는 비민감 코드만
표시한다. `none`은 `system` 또는 `clarify` 작업에만 허용한다. 업무 task에서 후보가
없으면 `specialist-unavailable`이다.

## 6. 구현 변경 범위

### 6.1 의도와 라우팅 통합

- `src/tybot/intent.py`
  - 기존 `plan()`의 복합 질문 분해를 `MasterPlanner`로 이전하거나 호환 어댑터로 감싼다.
  - 서로 충돌하는 “원문 조각 그대로”와 “독립적으로 이해되게” 지시를
    `original_fragment`와 `standalone_question`으로 분리한다.
  - 명시적 쓰기 명령과 상태/도움말의 결정적 우선 처리를 유지한다.
- 새 모듈 권장: `src/tybot/master_planner.py`
  - JSON 스키마 파싱, 허용 enum 검증, 후보 키 검증과 decision ID 생성을 담당한다.
  - planner LLM 장애 시 게이트웨이의 허용 모델 폴백을 먼저 사용한다.
  - 전부 실패하면 명확한 명령만 규칙 처리하고 업무 질문은 clarification/unavailable로
    닫는다. 규칙 기반 마스터 업무 답변을 만들지 않는다.
- `src/tybot/specialist_router.py`
  - `route()`의 두 번째 LLM 판정을 제거한다.
  - `MasterTask`와 레지스트리 스냅샷으로 결정적으로 전문 봇을 선택한다.
  - 기존 API를 즉시 삭제하지 말고 마이그레이션 기간 동안 호환 래퍼와 사용 중단 로그를
    둔다.

### 6.2 스레드 맥락 전달

- `src/tybot/slack/pilot.py`
  - `_thread_turns()` 결과에 QA record ID, 이전 질문, `subject_terms`, 문서 표시명과
    `evidence_refs` 존재 여부를 구조화해 planner에 전달한다.
  - `ThreadFollowupResolver`가 고른 부모 QA ID와 복원 결과를 `MasterTask`와 대조한다.
  - `specialist_hook(question, ...)` 대신 `specialist_hook(task, ...)` 형태로 바꾸고
    `standalone_question`을 전문 봇에 전달한다.
- `src/tybot/thread_followup.py`
  - LLM이 고른 부모 ID가 현재 스레드에 없으면 거부한다.
  - 복원된 근거는 반드시 현재 RequestContext의 채널/DM 범위와 ACL로 다시 연다.
  - 복원 실패 시 채널 전체 검색으로 조용히 넓히지 않는다.

### 6.3 마스터 직접 답변 제거

- `src/tybot/answer.py`
  - `factual`, `summary`, `analysis`, `advice`에서 전문 봇 실패 후 실행되는
    `self._router.complete()`를 제거한다.
  - 초기 검색 no-hit만으로 도구형 Hermes 호출을 막지 않는다. 빈 seed와 권한이 묶인
    ToolBox를 전달하고, 허용된 검색이 끝난 뒤 근거 부족 또는 검색 실패를 구분한다.
  - ACL 거부, 변환 실패, 전문 봇 unavailable은 코드가 상태 문구로 응답한다.
  - 검색과 근거 배분, coverage, 원본 링크 부착은 TYBot에 유지한다.
- `src/tybot/specialist_contract.py`
  - `fallback: Callable[[], str]`을 마스터 답변 생성기가 아니라 다음 후보 또는 실패 상태를
    표현하는 결과 타입으로 교체한다.
  - `success | unavailable | timeout | contract_violation | adapter_error`를 구분한다.

### 6.4 DB 행 매핑 수정

`src/tybot/specialist_router.py::available()`에서 조회한 `execution_mode`를
`Specialist(execution_mode=...)`에 전달한다. 구형 DB에 열이 없다는 주석만으로는 호환되지
않는다. 실제 열 존재 여부를 먼저 확인하거나, 스키마 버전을 요구하고 명확한 운영 오류로
처리한다. DB 오류를 빈 후보 목록과 동일하게 취급하지 않는다.

### 6.5 시각 근거

- 현재 `not visual.any` 조건으로 전문 봇을 건너뛰는 경로를 제거한다.
- 계약에 `VisualEvidenceRef`를 추가하고 ACL, OCR/PII 검사, 파일 크기/형식 검사를 통과한
  원본만 전달한다.
- prompt/tools/http 어댑터가 시각 입력을 지원하는지 capability로 선언한다.
- 미지원 전문 봇이면 다음 후보를 선택하고, 후보가 없으면 `visual-unsupported`로 닫는다.
- 마스터 LLM이 이미지를 직접 읽어 업무 답변을 만들지 않는다.

## 7. 감사와 콘솔

모든 업무 task는 전문 봇 후보가 없거나 planner가 실패한 경우도 추적한다.

```text
decision_id, qa_record_id, workspace, channel_id
task_kind, required_capability
planner_model, routing_confidence
selected_specialist, attempted_specialists
result, error_code, elapsed_ms, final_responder
```

- `final_responder`는 업무 task에서 승인 전문 봇 키 또는 `none`만 가능하다.
- 질문 본문, standalone 질문 본문, 근거 원문, 이미지와 시크릿은 감사 이벤트에 저장하지
  않는다.
- QA 레코드에는 현재처럼 사용자 질문과 전달 답변을 보관할 수 있지만, 전문 봇 호출
  감사 테이블에 중복 저장하지 않는다.
- 콘솔 답변 상세에는 “마스터 판정 → 근거 조회 → 전문 봇 시도 → 출력 검증 → 전송”을
  단계별로 보여주고, 마스터 직접 답변은 정책 위반으로 표시한다.

## 8. 구현 순서

Claude는 다음 순서를 지킨다. 한 단계가 테스트를 통과하기 전에 다음 단계로 넘어가지
않는다.

1. `execution_mode` DB 매핑, 어댑터 선택, ToolBox 연결을 복구하고 묵시적 prompt 강등을 제거한다.
2. 기존 planner와 호환되는 독립형 질문 계약을 연결하고 후속 미수금 질문을 검증한다.
3. 초기 검색 0건에서도 Hermes가 권한 내 검색·읽기로 자료를 찾도록 연결한다.
4. 세 문장 제한과 무조건적인 검색 재시도 금지를 업무별 출력·검색 예산 계약으로 바꾼다.
5. MasterDecision/MasterTask를 완성하고 전문 봇 선택을 결정적으로 통합한다.
6. 분류 불확실성·근거 부족·실행 실패를 분리하고 마스터 업무 답변을 제거한다.
7. 시각 입력 계약과 실패 체인을 검증한다.
8. 추적과 콘솔의 실제 실행 모드·도구 사용·실패 사유를 연결한다.
9. 지원 환경의 전체 테스트와 동일 자료 비교 평가를 수행한다.
10. 이중 라우터와 구 마스터 답변 코드를 제거하고 인계 결과를 남긴다.

Hermes 원본 저장소나 `ref/hermes`는 수정하지 않는다. TYBot 오케스트레이션, 공통 계약과
운영 연동 계약인 `subbots/hermes/contract/prompt.md`를 변경 대상으로 한다. 계약 변경은
버전과 출처를 남기고 기존 승인·배포 절차를 따른다. DB rules가 우선하는 경우도 확인한다.

## 9. 필수 테스트

### 9.1 맥락과 분류

- `미수금 현황을 확인해줘` 이후 `내가 이전에 요청했던 내용을 다시 확인해줘`가
  `미수금 현황을 다시 확인해줘`라는 독립형 질문으로 Hermes에 전달된다.
- `처리 안 된 하나의 문서`가 이전 응답의 전체 실패 목록이나 채널 전체로 확대되지 않는다.
- `주간 보고 내용을 종합해줘`, `여태까지 수집된 주간 보고를 정리해줘`가 같은
  capability와 Hermes로 간다. 기간만 각각 bounded/all 규칙대로 다르다.
- 복합 질문은 최대 3개 task로 유지하고 각 task가 독립형 질문을 가진다.
- 이전 봇 답변 문장이 planner 또는 전문 봇의 사실 근거가 되지 않는다.

### 9.2 마스터 비답변 불변식

- 전문 봇 없음, 레지스트리 DB 오류, planner 오류, 낮은 신뢰도, timeout, adapter 오류,
  빈 출력과 계약 위반에서 마스터 LLM의 업무 답변 호출 횟수가 0이다.
- specialist가 ACL 밖 source ID를 반환하면 답변을 폐기하고 마스터가 대신 답하지 않는다.
- `summary`, `factual`, `analysis`, `advice` 성공 응답의 `specialist`가 항상 비어 있지 않다.
- `status`, `help`, 명령 결과와 clarification은 LLM 업무 답변 없이 정상 동작한다.

### 9.3 권한

- 채널 질문은 현재 채널 근거만 사용한다.
- DM 질문만 사용자가 속한 허용 채널을 통합할 수 있다.
- root 워크스페이스 권한도 채널 질문 범위를 다른 채널로 확대하지 않는다.
- 후속 질문의 이전 근거 좌표는 현재 사용자의 ACL로 다시 검증된다.
- LLM이 임의의 parent ID, specialist key 또는 source ID를 만들면 거부된다.

### 9.4 전문 봇과 시각 근거

- DB 행의 `execution_mode=tools|http|prompt`가 런타임 객체에 그대로 매핑된다.
- `tools` Hermes에 RequestContext가 묶인 ToolBox가 제공된다.
- 시각 근거가 있어도 마스터가 답하지 않고 시각 capability 전문 봇에 전달된다.
- 시각 입력 미지원, PII 거부, 손상 파일은 비민감 실패 코드로 종료된다.
- 첫 전문 봇 실패 후 같은 capability의 두 번째 승인 전문 봇이 성공하는 체인을 검증한다.

### 9.5 감사

- 성공, 미선택, DB 오류와 모든 전문 봇 실패가 decision ID로 QA 레코드와 연결된다.
- 감사 이벤트에 질문/답변 본문, 근거 원문, 이미지, 토큰과 API 키가 포함되지 않는다.
- 콘솔에서 최종 응답 주체와 실패 단계를 구분할 수 있다.

## 10. 검증 명령과 완료 조건

```bash
pytest tests/test_thread_followup.py \
       tests/test_specialist_router.py \
       tests/test_specialist_contract.py \
       tests/test_answer_invariants.py \
       tests/test_tool_specialist.py
pytest
ruff check src tests scripts
cd console-web && npm run build
```

완료 조건:

- 위 테스트와 전체 테스트가 지원 환경에서 통과한다.
- 업무 답변 경로에서 마스터의 `Router.complete()` 호출이 없다는 회귀 테스트가 있다.
- 실제 운영 QA 한 건에서 독립형 미수금 후속 질문, Hermes 선택, 근거 복원, 출처 부착이
  하나의 decision ID로 연결된다.
- 같은 주간 보고 질문을 반복했을 때 전문 봇 선택이 일관된다.
- 전문 봇 장애를 강제로 만들었을 때 마스터 업무 답변 대신 명시적 unavailable이 나온다.
- 기존 문서 집합 coverage, 첨부 실패 안내와 원본 링크 기능이 유지된다.

## 11. Codex 재검증 체크리스트

Claude 구현 완료 후 Codex는 다음 순서로 검증한다.

1. 변경 커밋과 새 테스트가 이 문서의 각 요구사항을 실제로 검사하는지 코드 리뷰한다.
2. 마스터 직접 답변 호출을 정적 검색하고 허용된 system/clarify 경로만 남았는지 확인한다.
3. 후속 미수금, 주간 보고, 시각 첨부, 전문 봇 장애 시나리오를 집중 테스트한다.
4. 전체 pytest, Ruff와 콘솔 빌드를 실행한다.
5. 운영 전 실제 QA로 검증해야 하는 항목을 별도 verification 문서에 남긴다.
