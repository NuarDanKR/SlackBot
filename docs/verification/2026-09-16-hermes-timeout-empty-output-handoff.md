# Hermes timeout·빈 출력 장애 Claude 구현 인계

_작성: Codex / 2026-09-16_  
_상태: 원인 확인 완료, 구현 전_  
_우선순위: 긴급_  
_관련 설계: [`../design/master-specialist-orchestration.md`](../design/master-specialist-orchestration.md),
[`../design/operational-warning-recovery-and-answer-progress.md`](../design/operational-warning-recovery-and-answer-progress.md)_

## 1. 장애 증상과 판정

2026-09-16 운영 답변 현황에서 같은 종류의 문서 비교 질문이 다음처럼 실패했다.

| 시각 | 결과 | 소요 시간 | 근거 | 판정 |
|---|---|---:|---:|---|
| 13:11 | `specialist_unavailable`, `timeout` | 94.7초 | 20건 | Hermes 전체 실행 제한 초과 |
| 13:24 | `specialist_unavailable`, `invalid-output:empty` | 88.8초 | 20건 | Hermes가 유효한 text block 없이 종료 |

이는 전문 봇 미등록이나 근거 수집 실패가 아니다. 두 요청 모두 권한 필터를 통과한 근거
20건을 확보한 뒤 Hermes 실행 단계에서 실패했다. `specialist_unavailable`은 최종 사용자
상태이고, 실제 원인은 각각 `timeout`, `invalid-output:empty`다.

같은 시간대의 다음 오류는 별도 결함이다.

```text
SlackApiError: restricted_action_read_only_channel
질문처럼 들어온 본문: requested access to <Canvas URL> ...
```

Slack Canvas 접근 요청 시스템 메시지를 일반 DM 질문으로 처리하여 읽기 전용 대화에
`chat.postMessage`를 호출했다. Hermes 장애와 원인은 다르지만 같은 작업에서 함께 막는다.

## 2. 현재 구현에서 확인된 원인

### 2.1 시간 제한이 계층마다 분리되어 있다

- `specialist_router._run_one()`은 `execute(..., timeout_seconds=90)`을 사용한다.
- `ToolSpecialist`는 최대 8회의 LLM 도구 라운드와 상한 도달 후 최종 LLM 호출 1회를
  수행할 수 있다.
- 각 LLM 호출은 최대 8,192토큰이다.
- `ToolBudget.max_seconds=45`는 도구 실행 직전에만 확인한다. LLM 네트워크 호출에는
  적용되지 않는다.
- Gateway와 Anthropic/OpenAI Provider에는 요청 전체의 남은 시간이 전달되지 않는다.

따라서 각 상한은 하나의 deadline을 공유하지 않는다. 느린 LLM 호출 하나가 도구 예산을
넘겨도 계속 실행되고, 여러 라운드의 합이 90초를 넘는다.

### 2.2 `future.cancel()`은 실행 중인 Provider를 중단하지 않는다

`specialist_contract.execute()`는 `ThreadPoolExecutor`에서 adapter를 실행하고 90초 뒤
`future.cancel()`을 호출한다. Python은 이미 실행 중인 스레드를 취소하지 못한다.

그 결과 다음 위험이 있다.

- 사용자에게 timeout을 전달한 뒤에도 Provider 호출과 도구 루프가 계속될 수 있다.
- 늦게 끝난 호출 비용이 `specialist_call`에 정확히 기록되지 않을 수 있다.
- 반복 요청이 쌓이면 살아 있는 백그라운드 호출 수와 비용이 증가한다.
- timeout 뒤의 늦은 결과가 상태를 변경하거나 다음 도구 호출을 시작할 수 있다.

외부 90초 타이머만 늘리는 수정은 금지한다.

### 2.3 빈 출력의 정확한 원인이 기록되지 않는다

Anthropic Provider는 응답 content 중 `type=text`만 이어 붙인다. thinking/tool block만 있고
text block이 없으면 빈 문자열이다. 계약 검사는 이를 `invalid-output:empty`로 바꾸지만
다음을 보존하지 않는다.

- Provider `stop_reason`
- 실제 사용 모델과 Provider
- 완료한 LLM 라운드 수
- 도구별 호출 횟수와 마지막 도구
- input/output token 수
- 빈 출력 당시 tool call 존재 여부
- primary/recovery 중 어느 단계였는지

따라서 현재 기록만으로 `max_tokens`, thinking-only 응답, Provider 이상, 도구 루프 종료를
구별할 수 없다.

### 2.4 실패 복구가 없다

업무 답변은 전문 봇만 작성한다는 원칙 때문에 Hermes 실패 후 마스터가 답하지 않는 것은
올바르다. 그러나 같은 Hermes가 이미 읽은 근거로 답을 마무리하는 제한적 복구도 없어,
한 번의 빈 출력이나 지연이 전체 답변 실패가 된다.

## 3. 변경하지 않을 원칙

1. 마스터는 근거 기반 업무 답변을 작성하지 않는다.
2. 복구 답변의 응답 주체도 `hermes`다.
3. 복구 과정에서 권한 범위를 넓히거나 새 근거를 임의로 만들지 않는다.
4. 전문 봇 실패를 성공으로 위장하지 않는다.
5. timeout 뒤 늦게 도착한 결과는 게시하지 않는다.
6. 질문·근거·답변 원문을 새 metric이나 DB metadata에 복제하지 않는다.
7. Provider 장애를 이유로 더 낮은 보안 등급 모델에 기밀 자료를 보내지 않는다.

## 4. 목표 실행 모델

하나의 monotonic deadline을 요청 전체가 공유한다.

```text
전문 봇 총 deadline
  ├─ primary: Hermes 도구 탐색과 답변
  ├─ recovery: 이미 읽은 근거로 Hermes 무도구 최종화 1회
  └─ delivery reserve: QA 기록과 Slack 전달
```

권장 초기 기본값은 다음과 같다. 상수로 흩어 놓지 말고 한 설정 객체에서 검증한다.

| 항목 | 기본값 | 의미 |
|---|---:|---|
| 전체 전문 봇 deadline | 75초 | 사용자 대기 상한 |
| primary 종료 시점 | 55초 | recovery 시간을 남기는 경계 |
| Provider 1회 상한 | 25초 | `min(남은 시간, 25초)` |
| recovery 예약 | 최대 15초 | Hermes 무도구 호출 1회 |
| Slack·기록 예약 | 5초 | 늦은 답변 방지 |
| 도구 라운드 | 최대 4회 | 현재 8회에서 축소 |

수치는 운영 p50/p90 측정 후 조정하되 전체 deadline을 늘려 장애를 숨기지 않는다.

## 5. 구현 요구사항

### 5.1 deadline 객체를 한 번만 생성한다

요청별로 monotonic 기준의 deadline 객체를 만든다. 벽시계 `datetime.now()`를 timeout
계산에 사용하지 않는다.

필수 동작:

- `remaining()`은 항상 0 이상이다.
- `require_time(stage, reserve=...)`는 남은 시간이 부족하면 구조화된 timeout을 낸다.
- task, adapter, Gateway, Provider, tool loop가 같은 객체 또는 절대 deadline 값을 받는다.
- 재시도마다 75초를 새로 부여하지 않는다.

권장 위치는 `specialist_contract.py`의 작은 불변 객체다. 범용 Gateway API에는
`timeout_seconds` 또는 절대 deadline 중 하나만 전달해 의미를 중복시키지 않는다.

### 5.2 Provider에 실제 timeout을 전달한다

변경 대상:

- `src/tybot/gateway/base.py`
- `src/tybot/gateway/router.py`
- `src/tybot/gateway/providers/anthropic_provider.py`
- `src/tybot/gateway/providers/openai_provider.py`
- 필요하면 다른 등록 Provider

Gateway `complete()`는 남은 시간을 Provider까지 전달해야 한다. Provider SDK 호출에 실제
timeout을 설정한다. timeout 예외는 공통 `provider-timeout` 계열 코드로 정규화한다.

주의:

- Router가 fallback model을 시도할 때도 같은 deadline을 공유한다.
- 첫 모델이 남은 시간을 모두 썼으면 fallback model을 시작하지 않는다.
- timeout 뒤 새 모델·새 도구 호출은 0건이어야 한다.
- SDK timeout이 보장된 뒤에도 외부 안전장치가 필요하면 동시 실행 semaphore와 늦은 결과
  폐기를 함께 둔다. 취소할 수 없는 스레드를 요청마다 무제한 생성하면 안 된다.

### 5.3 ToolSpecialist가 매 라운드 deadline을 확인한다

변경 대상:

- `src/tybot/specialist_adapters.py`
- `src/tybot/specialist_tools.py`

각 LLM 호출 전, 각 도구 묶음 실행 전, 최종화 호출 전에 남은 시간을 확인한다.

- primary 경계를 넘으면 새 검색을 시작하지 않는다.
- 한 응답에 여러 tool call이 있으면 남은 시간과 도구 예산을 매 호출마다 다시 확인한다.
- 도구 결과가 충분하거나 이미 seed evidence가 있으면 불필요한 동의어 재검색을 줄인다.
- 라운드 상한은 4회로 낮춘다. 단순히 마지막 4개를 버리는 것이 아니라 primary 시간이
  끝나기 전에 최종화 단계로 이동한다.
- `ToolBudget.max_seconds`는 전체 deadline과 독립된 두 번째 시계가 되지 않도록 deadline을
  참조하거나 그보다 작은 단계 예산으로 명시한다.

### 5.4 Hermes 내부 복구는 한 번만 한다

복구 가능 조건:

- `invalid-output:empty`
- `stop_reason=max_tokens`이고 최종 text가 없음
- primary 단계 timeout이며 Provider 호출이 실제로 종료·취소됨
- 이미 허용된 seed evidence 또는 `toolbox.touched` 근거가 하나 이상 있음
- recovery 예약 시간이 남아 있음

복구 방식:

1. 새 검색·Slack 실시간 조회·문서 읽기를 금지한다.
2. 같은 Hermes 계약과 승인된 모델을 사용한다.
3. 이미 전달된 seed evidence와 실제로 읽은 `touched` 근거만 입력한다.
4. 질문에 직접 답하고 3,000자 이내로 마무리하도록 요청한다.
5. 한 번만 호출한다.
6. 성공 시 `final_responder=hermes`, `recovered=true`로 기록한다.

복구하지 않는 조건:

- 근거 0건
- ACL 또는 권한 오류
- Provider 인증·결제·모델 미등록 오류
- 계약상 출처 포함, 과도한 길이 등 빈 출력이 아닌 명시적 위반
- 남은 시간이 없음
- 원래 Provider 호출을 취소하지 못해 아직 실행 중임

복구 실패 시 기존처럼 `specialist_unavailable`을 반환한다. 마스터에게 업무 답변 생성을
시키지 않는다.

### 5.5 빈 출력 사유를 보존한다

`LLMResponse`에 이미 있는 `stop_reason`을 adapter 결과와 호출 기록까지 전달한다. 다음
metadata를 구조적으로 기록한다.

```text
phase=primary|recovery
actual_model
provider
stop_reason
rounds
tool_calls
tool_calls_by_name
output_chars
input_tokens
output_tokens
timeout_stage
recovered
late_result_discarded
```

질문·답변·도구 결과 본문은 metadata에 넣지 않는다.

`specialist_call.routing_reason` 200자 문자열에 모두 욱여넣지 않는다. `runtime_meta jsonb`
같은 구조화 필드를 추가하는 편이 낫다. 스키마를 추가한다면 다음을 함께 수정한다.

- SQL migration과 `deploy/apply-schema.sh`
- 기존 행 기본값 `{}`
- 애플리케이션 계정 `SELECT/INSERT` 권한
- `specialist_store.record_call()`과 조회 API
- 콘솔 전문 봇 호출 상세 화면
- 구형 스키마에서 배포 전 fail-fast 진단

DB 변경을 피한다면 최소한 QA `task_traces`와 로그에 같은 구조를 남겨야 하지만,
문자열 파싱을 새 계약으로 만들지는 않는다.

### 5.6 전문 봇 실패 표현을 분리한다

최종 `reason=specialist_unavailable`은 유지할 수 있으나 다음 실제 오류 코드는 보존한다.

```text
specialist-timeout:primary
specialist-timeout:recovery
invalid-output:empty
invalid-output:max-tokens
provider-timeout
provider-auth
search-budget-exhausted
evidence-insufficient
```

`invalid-output:empty`를 `timeout`으로 바꾸거나 반대로 합치지 않는다. 사용자 문구는 간단히
유지하되 콘솔에서 단계와 실제 오류를 확인할 수 있어야 한다.

## 6. Slack 읽기 전용 시스템 메시지 차단

변경 대상은 `src/tybot/slack/pilot.py`의 `on_message` DM 진입부다.

현재는 `bot_id`와 일부 `subtype`만 검사한다. Canvas 접근 요청처럼 Slack이 생성한 메시지가
일반 본문과 비슷한 형태로 들어오면 `_handle()`까지 도달한다.

구현 요구사항:

1. 사람 요청인지 판정하는 `_is_human_request(event, client)` 같은 단일 helper를 둔다.
2. `USLACKBOT`, bot/app user, hidden/system event는 답변 대상에서 제외한다.
3. `users_info`가 필요하면 사용자 ID별로 짧게 캐시하되 조회 실패 시 시스템 메시지를 사람
   메시지로 승격하지 않는 보수적 기본값을 사용한다.
4. `requested access to <Canvas URL>` 문자열 하나만 하드코딩한 필터로 끝내지 않는다.
5. 무시한 시스템 메시지는 QA 질문 기록과 아카이브에 넣지 않는다.
6. 실제 사람 DM, 첨부만 있는 `file_share`, 채널 `app_mention`은 계속 동작해야 한다.
7. 방어적으로 `restricted_action_read_only_channel` 전달 실패도 별도 오류 코드로 기록하되
   같은 읽기 전용 채널에 실패 메시지를 재게시하지 않는다.

## 7. 필수 테스트

### 7.1 deadline과 취소

- Provider가 받은 timeout이 `remaining()`보다 크지 않다.
- 첫 Provider가 deadline을 소진하면 fallback model 호출은 0회다.
- timeout 이후 도구 호출과 모델 호출이 추가되지 않는다.
- `execute()` 반환 후 카운터와 비용이 늦게 증가하지 않는다.
- 동시에 여러 timeout이 발생해도 살아 있는 specialist worker 수가 상한을 넘지 않는다.
- monotonic clock을 주입한 결정적 테스트를 사용하고 실제 90초 sleep 테스트는 만들지 않는다.

### 7.2 Hermes 복구

- primary의 빈 text + `stop_reason=max_tokens` + 근거 있음 -> 무도구 recovery 1회 성공.
- recovery 요청에는 tools가 없다.
- recovery는 기존 ACL 근거만 사용하고 새 검색을 호출하지 않는다.
- recovery 성공의 응답 주체는 Hermes다.
- 근거 0건이면 recovery 없이 `evidence-insufficient`다.
- recovery도 비면 `specialist_unavailable`이며 마스터 LLM 업무 답변 호출은 0회다.
- primary Provider가 아직 실행 중이면 중복 recovery를 시작하지 않는다.
- 성공·실패 모두 비용과 단계 metadata가 맞는다.

### 7.3 빈 출력 진단

- text 없음 + tool call 있음은 도구 라운드로 처리한다.
- text 없음 + tool call 없음 + `max_tokens`는 `invalid-output:max-tokens`다.
- text 없음 + tool call 없음 + 다른 stop reason은 `invalid-output:empty`다.
- console API에서 stop reason, rounds, tool count, output chars가 보인다.
- metadata에 질문·답변·근거 본문이 들어가지 않는다.

### 7.4 Slack 시스템 이벤트

- Canvas access request 이벤트를 주면 `_handle()`과 `chat.postMessage`가 호출되지 않는다.
- Slackbot/bot/app user 메시지가 답변 경로로 가지 않는다.
- 정상 사람 DM은 기존처럼 처리된다.
- 사람의 `file_share` DM은 첨부 처리와 답변 경로를 유지한다.
- 채널 app mention과 스레드 후속 질문은 회귀하지 않는다.
- 읽기 전용 채널 오류 처리 중 같은 채널로 오류 답변을 재전송하지 않는다.

## 8. 운영 검증

### 8.1 배포 전 현재 설정 확인

```sql
SELECT key, state, health, execution_mode, model, version,
       length(rules) AS rules_chars
  FROM specialist_bot
 WHERE key = 'hermes';
```

Hermes는 `execution_mode=tools`여야 한다. 모델이 빈 값이면 Gateway 기본 모델이 사용되므로
운영 진단에는 해석된 실제 모델도 표시해야 한다.

최근 실패 호출을 확인한다.

```sql
SELECT at, workspace, specialist, result, elapsed_ms, error_code,
       routing_reason, qa_record_id
  FROM specialist_call
 WHERE specialist = 'hermes'
   AND at >= now() - interval '1 day'
 ORDER BY at DESC;
```

### 8.2 배포 후 smoke test

같은 사용자·권한·채널에서 다음을 각각 한 번 실행한다.

1. 근거 20건의 현장 간 비교 질문
2. 단일 문서 수치 확인
3. 같은 스레드의 형식 변경
4. 근거가 실제로 없는 질문
5. Provider 지연을 주입한 테스트 환경 timeout
6. Canvas 접근 요청 시스템 이벤트 fixture

확인 항목:

- 성공 답변의 수치와 출처가 기존 근거에 존재한다.
- 전체 처리 시간이 설정 deadline 이내다.
- timeout 후 추가 `llm_call` 로그가 없다.
- `specialist_call`에 primary/recovery와 실제 모델이 남는다.
- 복구 성공도 Hermes 답변으로 표시된다.
- Slack 시스템 이벤트에 답하지 않는다.

### 8.3 배포 판단 지표

변경 전후 최소 30건 또는 3영업일을 비교한다.

- Hermes 성공률
- `timeout` 비율
- `invalid-output:empty` 비율
- recovery 시도와 성공률
- p50/p90 응답 시간
- 질문당 LLM 호출 수와 비용
- timeout 뒤 늦은 호출 수
- `restricted_action_read_only_channel` 발생 수

성공률만 높이고 비용이나 p90이 크게 악화되면 완료로 보지 않는다.

## 9. 구현 순서

1. 현재 실패 fixture와 Provider timeout 전달 테스트를 먼저 추가한다.
2. 공통 deadline과 Provider timeout을 구현한다.
3. ToolSpecialist 라운드와 단계 예산을 deadline에 연결한다.
4. stop reason과 안전한 runtime metadata를 보존한다.
5. Hermes 무도구 recovery 1회를 추가한다.
6. Slack 사람/시스템 이벤트 판정을 추가한다.
7. 콘솔 호출 상세와 스키마 진단을 연결한다.
8. 전체 specialist·master orchestration·Slack 테스트와 Ruff를 실행한다.
9. 운영 smoke test 결과를 별도 구현 기록 문서에 남긴다.

## 10. 완료 조건

- 90초를 늘리는 방식이 아니라 Provider까지 실제 deadline이 전달된다.
- timeout 이후 추가 모델·도구 호출과 늦은 결과 게시가 0건이다.
- 근거가 있는 빈 출력은 Hermes 내부에서 한 번 복구할 수 있다.
- 복구 실패 시 마스터가 업무 답변을 만들지 않는다.
- `timeout`과 `invalid-output`의 단계·stop reason을 콘솔에서 구분한다.
- Canvas 접근 요청 시스템 메시지를 질문으로 처리하지 않는다.
- 질문·답변·근거 원문이 새 metadata에 복제되지 않는다.
- 관련 단위·통합 테스트, 전체 pytest, Ruff, pre-commit guard가 통과한다.

## 11. Codex 재검증 요청

Claude는 완료 후 다음을 남긴다.

- 변경 커밋 SHA
- 변경 파일과 스키마 목록
- 전체 테스트 결과
- deadline 기본값과 조정 근거
- 실패 fixture별 결과
- 운영 smoke test 또는 아직 운영에서 확인하지 못한 항목

Codex는 특히 다음 세 가지를 독립적으로 다시 확인한다.

1. timeout 후 실제 Provider 작업이 남지 않는가.
2. recovery가 마스터 답변이나 권한 확대 경로가 아닌가.
3. Slack 시스템 이벤트 차단이 정상 사람 DM과 첨부를 막지 않는가.
