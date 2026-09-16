# Hermes timeout 근본 수정 및 근거 보기 UX - Claude 구현 인계

_작성: Codex / 2026-09-16_  
_상태: 원인 확인 완료, 구현 대기_  
_우선순위: 긴급_  
_선행 구현: [`2026-09-16-hermes-timeout-empty-output-implementation.md`](2026-09-16-hermes-timeout-empty-output-implementation.md)_  
_운영 증거: `qa-log/answer-issues-2026-09-16-all.md` (원문/감사 자료이므로 커밋 금지)_

## 1. 목표

다음 두 운영 장애를 한 작업에서 해결한다.

1. Hermes가 이미 근거 20건을 받은 비교·분석 질문에서 74~76초 만에
   `specialist-timeout:primary`로 끝난다.
2. 실패 답변에도 `근거 보기`가 표시되고, 클릭하면 별도 ephemeral 메시지만 생겨 원래 화면으로
   돌아가는 동작이 없다. 답변 당시 근거가 아니라 같은 검색어로 다시 검색하는 문제도 있다.

이번 작업은 **기본 timeout을 충분히 늘리는 긴급 완화**와 **검색이 최종 답변 작성 시간을
소진하지 못하게 하는 구조 수정**을 모두 포함한다. timeout 숫자만 늘리고 완료 처리하지 않는다.

## 2. 확인된 사실

동일하거나 사실상 같은 질문의 운영 결과는 다음과 같다.

| 시각 | 결과 | 전체 경과 | 근거 |
|---|---|---:|---:|
| 13:03 | `answered` | 77,274ms | 20건 |
| 14:23 | `specialist-timeout:primary` | 76,244ms | 20건 |
| 14:31 | `specialist-timeout:primary` | 74,501ms | 20건 |

질문 예시:

> 부산항 신항 웅동지구 현장 외주업체 손익의 기간별 변화 내용을 분석해서 잘 설명해주세요.

13:03 성공 답변에는 `검색 예산 소진`이 명시돼 있다. 수집·ACL 실패가 아니라 Hermes의 반복 검색과
최종 합성 단계가 느린 사례다.

현재 `SpecialistDeadlines` 기본값은 다음과 같다.

```text
total=75
primary=55
per_call=25
recovery=15
reserve=5
```

`execute()`가 기다리는 값은 `deadline.remaining()`이고 이 값은 `reserve=5`를 제외한다. 따라서
전문 봇의 실질 외부 대기 상한은 70초다. 콘솔의 74.5초는 모델 시간만이 아니라 계획, ACL,
초기 검색, 전문 봇 실행과 실패 응답 처리까지 포함한 요청 전체 경과다. “90초 전에 실패했다”가
아니라 배포 후 실효 한도가 70초로 낮아진 회귀다.

## 3. 시간 예산 계약

### 3.1 운영 기본값을 180초로 올린다

기본값은 아래와 같이 변경한다. 사용자가 상세 분석을 요청한 경우 3분까지 기다릴 수 있게 하되,
각 단계가 다음 단계를 침범하지 못하게 한다.

| 단계 | 기본 예산 | 의미 |
|---|---:|---|
| 전체 specialist | 180초 | adapter 진입부터 결과 반환까지의 절대 상한 |
| 탐색·도구 사용 | 100초 | 추가 검색과 문서 열람을 새로 시작할 수 있는 구간 |
| 최종 합성 | 45초 | 이미 읽은 근거로 답변을 만드는 무도구 호출 1회 |
| 복구 합성 | 20초 | 허용된 실패에서 이미 읽은 근거로 무도구 재시도 1회 |
| 반환 여유 | 15초 | QA 기록, Canvas/Slack 전송을 위한 specialist 외부 여유 |
| Provider 1회 | 최대 45초 | 해당 단계 잔여 시간과 45초 중 작은 값 |

합계 계약은 `100 + 45 + 20 + 15 = 180`이다. 시간값은 하나의 설정 객체에서 검증하고,
서로 독립된 상수로 흩어 놓지 않는다.

`primary`라는 이름이 탐색과 최종 합성을 함께 뜻해 혼동을 만든다. 가능하면 아래처럼 명시적인
필드로 바꾼다.

```python
SpecialistDeadlines(
    total=180.0,
    discovery=100.0,
    finalize=45.0,
    recovery=20.0,
    delivery_reserve=15.0,
    per_call=45.0,
)
```

호환 때문에 즉시 필드명을 바꾸기 어렵다면 외부 API는 유지할 수 있지만, 내부 deadline은
`discovery_deadline`, `finalize_deadline`, `recovery_deadline`, `hard_deadline`을 각각 계산해야 한다.

### 3.2 절대 deadline 하나만 사용한다

- `time.monotonic()` 기준 시작 시각은 한 번만 만든다.
- adapter, Gateway, Provider와 도구가 같은 절대 deadline을 본다.
- 단계가 바뀔 때 45초나 20초를 새로 더하지 않는다.
- Provider timeout은 `min(단계 잔여 시간, per_call)`이다.
- 전체 180초가 끝난 뒤 새 모델 호출, 도구 호출, fallback 모델 호출은 0건이어야 한다.
- Python future timeout과 Provider SDK timeout을 구분해 기록한다.

### 3.3 최종 합성 시간을 선점한다

탐색 단계가 100초를 다 쓰거나 도구 라운드 상한에 도달하면 실패시키지 말고, 지금까지 읽은
근거로 `finalize`에 진입한다.

- `_finalize()` 진입 전에 `adapter.phase = "finalize"`를 반드시 설정한다.
- finalize 호출에는 tools를 전달하지 않는다.
- finalize가 사용할 45초를 discovery가 빌려 쓰지 못한다.
- finalize 성공 후 recovery를 실행하지 않는다.
- finalize가 빈 출력, `max_tokens`, 취소 완료된 Provider timeout이고 읽은 근거가 있으면
  recovery를 1회만 실행한다.
- discovery 중 timeout도 Provider 작업이 실제 종료됐고 읽은 근거가 있으면 finalize 또는
  recovery로 넘어갈 수 있어야 한다. `deadline.aborted` 하나만으로 복구를 막지 않는다.
- auth, billing, ACL, 모델 미등록, 근거 0건은 복구하지 않는다.

현재 `RECOVERABLE`에 `specialist-timeout:primary`가 없고 `deadline.aborted`면 무조건 복구를
막는다. 이 조건을 단계별 상태로 교체해야 한다.

## 4. 검색과 문서 범위 축소

시간을 180초로 늘려도 Hermes가 같은 광범위 검색을 반복하면 비용과 p90만 늘어난다.

### 4.1 seed-first

마스터가 이미 전달한 `request.evidence`를 우선 사용한다.

- seed가 있고 질문의 핵심 엔터티·기간을 포함하면 첫 호출은 무도구 답변 가능 여부를 판단한다.
- seed가 충분하면 전체 아카이브 검색을 다시 시작하지 않는다.
- 부족한 필드가 명시된 경우에만 추가 검색을 허용한다.
- 추가 검색어는 질문의 현장명·문서명·기간을 유지해야 한다.

“whatever you seek, start here”처럼 무조건 검색하게 하는 Hermes 도구 설명·프롬프트가 있다면
seed가 없는 경우에만 적용하도록 바꾼다. Hermes 원본 프로젝트를 수정하지 않고 TYBot의 governed
prompt와 도구 제공 조건에서 해결한다.

### 4.2 중복 제거와 범위 고정

- Slack `file_id`, 원문 좌표와 canonical source ID로 원본·변환본 중복을 제거한다.
- 동일 파일이 다른 수집 경로에 있어도 한 후보로 취급한다.
- 현재 채널 질문은 현재 채널의 일치 문서를 먼저 사용한다.
- 부분 단어 검색 결과를 곧바로 “읽은 근거”로 간주하지 않는다.
- `toolbox.touched`에는 실제 본문을 연 문서만 넣는다. 검색 결과에 나타났다는 이유만으로 출처에
  추가하지 않는다.
- search 결과 최대 30건을 그대로 모델에 주지 말고, 정규화·중복 제거 후 상위 후보만 전달한다.

### 4.3 라운드와 토큰

- 새 검색을 시작할 수 있는 LLM 라운드는 최대 2회로 제한한다.
- 남은 라운드는 문서 열람 또는 최종 합성에만 쓴다.
- 도구 선택 호출과 최종 답변 호출의 `max_tokens`를 분리한다.
- 도구 선택은 2,048~4,096 토큰, 최종 합성은 3,000자 답변에 맞는 상한을 사용한다.
- `max_tokens=8192`를 모든 호출에 동일하게 적용하지 않는다.

## 5. 오류 코드와 관측성

다음 단계를 별도로 기록한다.

```text
timeout_stage=discovery|finalize|recovery|total|provider
deadline_total_ms
elapsed_before_specialist_ms
specialist_elapsed_ms
discovery_elapsed_ms
finalize_elapsed_ms
recovery_elapsed_ms
rounds
tool_calls
tool_calls_by_name
seed_count
opened_evidence_count
deduplicated_candidate_count
actual_model
provider
stop_reason
recovered
late_result_discarded
```

- 콘솔의 현재 “모델 실행”은 요청 전체 경과와 혼동된다. `요청 전체`, `전처리`, `전문 봇`,
  `최종 합성`을 구분해 표시한다.
- QA packet에 `specialist_call`의 runtime metadata를 포함한다. 질문·답변·도구 결과 본문은 넣지 않는다.
- `answer_issues.issue_codes()`가 trace result `specialist_unavailable`도 전문 봇 실패로 집계해야 한다.
- `specialist-timeout:primary`처럼 어느 단계인지 모호한 신규 기록은 생성하지 않는다. 기존 기록의
  조회 호환성은 유지한다.

## 6. `근거 보기` 수정

### 6.1 버튼 표시 조건

다음 조건을 모두 만족할 때만 버튼을 표시한다.

- `ans.reason == "answered"`
- 최종 답변이 실제 근거를 사용함
- 저장 가능한 `EvidenceRef`가 1건 이상 있음
- 현재 사용자가 다시 열람할 권한이 있음

`specialist_unavailable`, timeout, 오류, 근거 없음 답변에는 버튼을 붙이지 않는다. 검색어
`ans.terms`만 있다는 이유로 버튼을 만들지 않는다.

### 6.2 재검색 대신 답변 당시 좌표 사용

현재 버튼은 검색어를 value에 넣고 클릭 시 다시 검색한다. 이를 폐기한다.

- 버튼에는 원문이나 검색어 대신 짧은 `qa_record_id` 또는 서명된 불투명 ID를 넣는다.
- 서버에서 답변 당시 `EvidenceRef` 목록을 조회한다.
- 클릭 시점의 워크스페이스·채널 ACL로 각 ref를 다시 연다.
- 권한이 사라진 ref는 표시하지 않는다.
- 봇 답변과 요약을 근거로 되먹이지 않는다.

### 6.3 Slack 모달

`respond(..., response_type="ephemeral")` 대신 `views_open`으로 근거 모달을 연다.

- 모달 제목: `답변 근거`
- 본문: 출처, 문서명, 날짜, 원문 일부. 기존 원문 노출 상한을 유지한다.
- 닫기 버튼은 Slack 기본 `닫기`를 사용한다.
- 닫으면 원래 스레드가 그대로 보이므로 별도 “이전” 상태 관리가 필요 없다.
- `trigger_id`가 없거나 모달 생성이 실패한 경우에만 ephemeral 폴백을 사용한다.
- 폴백에는 `닫기` 버튼을 제공하고 `respond(delete_original=True)`로 제거할 수 있게 한다.

## 7. 필수 테스트

실제 sleep 없이 가짜 monotonic clock과 가짜 Provider를 사용한다.

### 7.1 deadline

- 기본 총 시간이 180초다.
- discovery 100초 뒤 새 검색·새 도구 호출이 시작되지 않는다.
- discovery 종료 후 finalize 45초가 보존된다.
- finalize 실패 후 허용된 경우에만 recovery 20초를 한 번 사용한다.
- delivery reserve 15초를 specialist가 사용하지 않는다.
- Provider가 받는 timeout은 해당 단계 잔여 시간과 45초보다 크지 않다.
- hard deadline 이후 모델·도구 호출 및 늦은 결과 게시가 없다.
- 단계별 오류 코드와 elapsed metadata가 정확하다.

### 7.2 검색

- seed가 충분하면 search tool 호출 0건으로 답변한다.
- seed가 부족하면 추가 검색은 최대 2라운드다.
- 같은 Slack file의 원본·변환본은 한 후보로 전달된다.
- 검색 결과에만 나타나고 열지 않은 문서는 출처에 포함되지 않는다.
- 관련 파일 2건 fixture에서 다른 현장·채널 문서가 최종 근거로 섞이지 않는다.

### 7.3 운영 회귀 fixture

QA packet의 부산항 신항 웅동지구 질문을 개인정보·원문 없이 재현하는 합성 fixture를 만든다.

- 이전 77초 처리 패턴에서도 timeout이 발생하지 않는다.
- 답변 주체는 Hermes다.
- 수치와 기간은 fixture 원문 그대로다.
- 답변은 3,000자 이하다.
- 성공 답변의 출처는 실제로 연 후보만 포함한다.

### 7.4 근거 보기

- 성공 + `EvidenceRef` 있음: 버튼 표시.
- timeout, `specialist_unavailable`, 근거 없음: 버튼 미표시.
- 버튼 value에 질문·원문·검색어를 넣지 않는다.
- 클릭 시 재검색하지 않고 저장된 ref를 현재 ACL로 다시 연다.
- 모달 닫기 후 원래 스레드가 유지된다.
- 모달 실패 시 ephemeral 폴백을 닫을 수 있다.
- 권한이 회수된 문서는 표시되지 않는다.

### 7.5 정적·전체 검증

```bash
pytest tests/test_specialist_deadline.py \
       tests/test_specialist_contract.py \
       tests/test_specialist_router.py \
       tests/test_specialist_tools.py \
       tests/test_evidence_view.py
ruff check src tests scripts
pytest
```

## 8. 운영 검증

배포 후 동일 사용자·동일 채널에서 다음을 확인한다.

1. 부산항 신항 웅동지구 기간별 손익 분석 질문을 3회 실행한다.
2. 단일 문서 수치 확인, 짧은 후속 편집, 근거 없는 질문을 각각 실행한다.
3. 성공 답변에서 `근거 보기`를 열고 닫는다.
4. 강제 Provider 지연 fixture에서 discovery/finalize/recovery timeout을 각각 확인한다.

판정 기준:

- 동일 분석 질문 3회 중 timeout 0건.
- 전체 처리 시간은 180초 미만이며 단계별 시간이 콘솔에 합리적으로 표시됨.
- 관련 파일만 근거로 사용되고 중복 출처가 없음.
- timeout 후 추가 `llm_call`과 tool call이 0건.
- 실패 답변에 `근거 보기` 버튼이 없음.
- 성공 답변의 모달을 닫으면 원래 스레드로 즉시 돌아감.

운영 30건 또는 3영업일 동안 성공률, p50/p90, timeout 비율, 평균 모델 호출 수와 비용을 비교한다.
180초 상향으로 성공률은 개선됐지만 p90·비용만 크게 늘면 검색 제한이 제대로 구현되지 않은 것이다.

## 9. 변경 금지 사항

- timeout을 없애거나 무제한으로 만들지 않는다.
- 마스터가 근거 기반 업무 답변을 대신 생성하게 하지 않는다.
- timeout 복구 과정에서 권한 범위를 넓히거나 실시간 재검색하지 않는다.
- 질문, 답변, 검색 결과 본문을 runtime metadata에 복제하지 않는다.
- `qa-log/`를 커밋하지 않는다.
- 3,000자 답변 상한을 timeout 해결 명목으로 되돌리지 않는다.

## 10. Claude 완료 보고 형식

Claude는 구현 후 아래를 이 문서 또는 별도 구현 기록에 남긴다.

- 변경 커밋 SHA와 변경 파일 목록
- 실제 적용한 deadline 기본값과 단계 합계
- seed-first 및 중복 제거 동작 설명
- timeout fixture별 호출 횟수와 오류 코드
- 근거 모달과 ACL 재검증 방식
- 대상 테스트, 전체 pytest, Ruff 결과
- 서버 배포 후 필요한 명령 및 스키마 변경 여부
- 아직 운영에서만 검증할 수 있는 항목

Codex는 이후 코드와 테스트를 독립적으로 검토하고, QA packet의 동일 질문 운영 결과까지 확인한다.
