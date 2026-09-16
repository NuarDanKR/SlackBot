# Hermes timeout·빈 출력 장애 구현 기록

> 작성: 2026-09-16 (Claude)
> 인계: [`2026-09-16-hermes-timeout-empty-output-handoff.md`](2026-09-16-hermes-timeout-empty-output-handoff.md)
> 상태: 구현 완료 · **운영 smoke test 대기**

## 1. 변경 파일

### 새로 만든 것

| 파일 | 하는 일 |
|---|---|
| `src/tybot/gateway/providers/_timeout.py` | SDK 별 timeout 예외를 이름으로 알아본다 |
| `tests/test_specialist_deadline.py` | 시간 예산·복구·빈 출력 진단 21건 |
| `tests/test_slack_system_events.py` | 시스템 이벤트 차단과 **배선** 23건 |

### 고친 것

| 파일 | 무엇을 |
|---|---|
| `src/tybot/specialist_contract.py` | `SpecialistDeadlines`·`Deadline`·`StageTimeout`, 동시 호출 상한, 늦은 결과 폐기, 빈 출력 사유 분리 |
| `src/tybot/gateway/base.py` | `ProviderTimeout`, Provider 프로토콜에 `timeout_seconds` |
| `src/tybot/gateway/router.py` | 폴백 후보가 **같은 시계**를 공유, Provider 에 남은 시간 전달 |
| `src/tybot/gateway/providers/anthropic_provider.py` | SDK `timeout` 설정, timeout 예외 정규화 |
| `src/tybot/gateway/providers/openai_provider.py` | 같음 |
| `src/tybot/specialist_adapters.py` | 라운드 8→4, 매 라운드·도구마다 시계 확인, `recover()`, stop_reason·토큰 보존 |
| `src/tybot/specialist_router.py` | 무도구 복구 1회, 런타임 진단값 기록 |
| `src/tybot/slack/pilot.py` | `route_message()`·`_is_human_request()`·`_is_bot_user()` |

**DB 스키마 변경 없음.** 인계 §5.5 가 허용한 대로 `specialist_call.routing_reason`
에 비민감 값만 더했다. `runtime_meta jsonb` 는 다음에 스키마를 손댈 때 옮긴다.

## 2. 시간 예산 기본값과 근거

`SpecialistDeadlines` 한 객체에서 **검증까지** 한다. 상수로 흩어 두면 합이 안 맞는
조합이 조용히 들어간다 — `primary + recovery + reserve > total` 이면 생성 자체가
거부된다.

| 항목 | 값 | 왜 |
|---|---:|---|
| 전체 | 75초 | 사용자 대기 상한. 인계 §4 권장값 |
| primary | 55초 | 이 뒤로는 **새 검색을 시작하지 않는다** |
| Provider 1회 | 25초 | `min(남은 시간, 25)` |
| recovery | 15초 | 무도구 최종화 1회 |
| reserve | 5초 | QA 기록·Slack 전달 |
| 도구 라운드 | 4회 | 8회에서 축소 |

**90초를 늘리지 않았다.** 오히려 75초로 줄었다 — 늘리는 것은 장애를 숨긴다.

수치는 운영 p50/p90 측정 뒤 조정한다. 조정하더라도 전체를 늘리기 전에 라운드 수와
per_call 을 먼저 본다.

## 3. 핵심 설계 판단

### primary 경계는 **끝이 아니라 전환**이다

처음에는 `require_time("primary")` 가 예외를 올리게 했다. 테스트가 잡았다 —
그러면 이미 읽은 근거로 마무리할 기회까지 사라진다.

```text
remaining_primary() <= 0  →  새 검색 안 함, 최종화로 이동   (break)
remaining() <= 0          →  최종화도 못 함                  (StageTimeout)
```

두 상황의 사유 코드가 다르다(`specialist-timeout:total` vs 최종화 성공).

### 늦은 결과는 게시하지 않는다

Python 은 실행 중인 스레드를 못 죽인다. `future.cancel()` 은 시작 전 작업만 취소한다.

그래서 **표식을 세운다** — `deadline.abort(stage)`. 어댑터는 매 라운드·매 도구
앞에서 그것을 보고 스스로 멈추고, `execute()` 는 시간이 끝난 뒤 도착한 결과를
`specialist-timeout:*` 으로 버린다.

살아 있는 호출 수도 막는다(`MAX_LIVE_CALLS=8`). 상한이 없으면 timeout 이 쌓일수록
Provider 호출과 비용이 함께 늘어난다.

### 복구는 **같은 Hermes**, 도구 없이, 한 번

입력은 실패한 회차의 대화(`_transcript`)뿐이다. 그 안에는 이미 권한을 통과한
근거만 들어 있다 — 복구가 권한을 넓히는 길이 되면 안 된다.

복구 호출도 `execute()` 를 지나 **같은 계약 검사**를 받는다(`_RecoveryAdapter`).
복구 답변이라고 출처를 붙이거나 길이 상한을 건너뛰면 그 경로만 검사가 헐거워진다.

복구하지 않는 조건:

| 상황 | 왜 |
|---|---|
| 사유가 `:empty`·`:max-tokens` 가 아님 | 출처 포함·길이 초과는 다시 물어도 같은 답 |
| 프롬프트형 어댑터 | 되살릴 대화가 없다 — 그냥 재시도지 복구가 아니다 |
| 시간 없음·이미 abort | 원래 호출이 살아 있을 수 있어 두 호출이 겹친다 |
| 근거 0건 | 읽은 것이 없는데 마무리하라면 자료 없이 문장을 만든다 |

복구가 실패하면 `invalid-output:empty+recovery-failed` 로 닫고 **마스터가 대신
답하지 않는다.**

### Slack 판정은 배선까지 테스트한다

`on_message` 클로저 안에 판정을 두면 테스트가 그 갈래를 지나갈 수 없다.
되돌리기 실험에서 **호출을 통째로 지워도 전부 통과했다.** 그래서
`route_message()` 메서드로 꺼내고, Bolt 가 등록한 핸들러를 직접 부르는 테스트를
따로 뒀다.

`_is_bot_user()` 는 조회 실패 시 답변 경로를 닫되 실패 결과를 캐시하지 않는다.
일시적인 `users.info` 실패로 사람 메시지를 한 번 놓치는 것보다 시스템 메시지를 사람
질문으로 승격해 읽기 전용 대화에 답하고 QA 기록을 오염시키는 위험을 우선 차단한다.

## 4. 새 오류 코드

```text
specialist-timeout:primary     primary 단계에서 끝남
specialist-timeout:total       전체 시간이 끝나 최종화도 못 함
specialist-timeout:recovery    복구 단계에서 끝남
specialist-busy                동시 호출 상한 — 스레드를 만들지 않았다
invalid-output:empty           text 없음, stop_reason 은 다른 것
invalid-output:max-tokens      text 없음 + 토큰 상한에서 끊김
invalid-output:sources         출처 구역 포함
invalid-output:too-long        본문 상한 초과
provider-timeout               Provider SDK timeout(정규화)
…+recovery-failed              복구까지 실패
```

호출 기록(`routing_reason`)에 함께 남는 비민감 값:

```text
phase=primary|recovery  stop_reason=…  rounds=N  in_tok=N  out_tok=N
output_chars=N  recovered=1
```

**질문·답변·도구 결과 본문은 넣지 않는다.**

## 5. 테스트

```text
pytest                        2426 passed
ruff check src tests scripts  All checks passed
```

실제로 재우지 않는다 — monotonic 시계를 주입해 결정적으로 돌린다. 90초를 자는
테스트는 아무도 안 돌리게 되고, 안 돌리는 테스트는 없는 것과 같다.

### 되돌리기 실험 — 11건 전부 잡힘

| 되돌린 것 | 결과 |
|---|---|
| 늦게 온 결과를 그대로 게시 | 1 failed ✅ |
| 단계 시간 확인 제거 | 3 failed ✅ |
| `max_tokens` 구별 제거 | 1 failed ✅ |
| 동시 호출 상한 제거 | 1 failed ✅ |
| 시간 없어도 폴백 시작 | 1 failed ✅ |
| Provider 에 timeout 미전달 | 2 failed ✅ |
| primary 경계 무시 | 2 failed ✅ |
| 도구 묶음 중간 재확인 제거 | 1 failed ✅ |
| 시스템 메시지 차단 제거 | 1 failed ✅ |
| 시스템 subtype 표식 제거 | 1 failed ✅ |
| **배선 자체를 제거** | 1 failed ✅ |

마지막 둘은 **처음에 안 잡혔다.** 판정 함수만 테스트했기 때문이다 — 그래서
`route_message()` 를 꺼내고 배선 테스트를 더했다.

### 구현 중 테스트가 잡은 실제 결함 2건

1. **`router.complete()` 의 `UnboundLocalError`** — 남은 시간이 없을 때 `break` 로
   빠지면 `for…else` 를 건너뛰어 `resp` 없이 아래로 내려갔다. 예외로 바꿨다.
2. **primary 경계에서 최종화 기회 상실** — `require_time` 이 예외를 올려 이미 읽은
   근거로 마무리할 수 없었다. 경계는 전환, 전체 종료만 예외로 나눴다.

## 6. 아직 확인하지 못한 것 — 운영

인계 §8.2 의 smoke test 는 **실제 Slack·Provider 가 필요해 돌리지 못했다.**

- [ ] 근거 20건의 현장 간 비교 질문이 75초 안에 끝나는지
- [ ] timeout 뒤 추가 `llm_call` 로그가 0건인지
- [ ] `specialist_call` 에 `phase=`·실제 모델이 남는지
- [ ] 복구 성공이 Hermes 답변으로 표시되는지
- [ ] Canvas 접근 요청 이벤트에 답하지 않는지
- [ ] `restricted_action_read_only_channel` 발생 수가 0 이 되는지

배포 전 확인(인계 §8.1):

```sql
SELECT key, state, health, execution_mode, model, version,
       length(rules) AS rules_chars
  FROM specialist_bot WHERE key = 'hermes';
```

`execution_mode=tools` 여야 한다. `prompt` 면 복구가 동작하지 않는다 —
프롬프트형에는 되살릴 대화가 없기 때문이다.

### 판단 지표(§8.3)

변경 전후 최소 30건 또는 3영업일. **성공률만 올리고 p90·비용이 나빠지면 완료가
아니다.** 라운드를 8→4 로 줄였으므로 도구 호출 수와 비용은 내려가는 쪽이 정상이고,
그렇지 않으면 시간이 아니라 다른 곳이 원인이다.

## 7. 하지 않은 것

- **90초를 늘리지 않았다.** 75초로 줄였다
- `runtime_meta jsonb` 스키마를 추가하지 않았다 — 인계가 허용한 문자열 경로를 썼다.
  다음에 스키마를 손댈 때 옮긴다
- 마스터가 업무 답변을 만드는 경로를 만들지 않았다
- 복구를 두 번 이상 하지 않는다
- `specialist_wire`(HTTP 어댑터)의 상한은 손대지 않았다 — 거기는 컨테이너에
  상한을 요청에 실어 보내는 구조라 상황이 다르다

## 8. Codex 재검증 요청 항목에 대한 답

1. **timeout 후 Provider 작업이 남는가** — SDK 에 실제 `timeout` 을 걸었고,
   폴백 후보는 같은 시계를 본다. 스레드는 못 죽이므로 `abort` 표식 + 동시 호출
   상한 + 늦은 결과 폐기를 함께 뒀다. 완전한 취소는 아니며, 그 한계를 코드
   주석에 적었다.
2. **복구가 마스터 답변·권한 확대인가** — 아니다. 같은 Hermes 계약, 같은
   `execute()` 검사, 입력은 실패한 회차의 대화뿐이고 도구를 주지 않는다.
   테스트가 `toolbox.ran == []` 를 확인한다.
3. **시스템 이벤트 차단이 사람 DM·첨부를 막는가** — 정상 조회 시 사람 DM,
   `file_share` DM, 채널 메시지, 정정 단축 경로를 각각 테스트로 지난다.
   `users.info` 조회 실패는 보수적으로 답변 경로를 닫고 실패 결과는 캐시하지 않는다.

## 9. Codex 재검증 보완 (2026-09-16)

초기 구현 검토에서 확인된 다음 결함을 보완했다.

- Canvas 실행 거부 보정 호출이 새 90초 예산을 만들지 않고 최초 deadline을 재사용
- `ProviderTimeout`을 외부 future timeout과 분리해 `provider-timeout`으로 기록
- 복구 단계의 외부 timeout을 `specialist-timeout:recovery`로 기록
- PromptSpecialist와 후속 편집 호출에도 Provider timeout 전달
- 어댑터 생성 실패 시 `recovered` 미초기화로 원래 오류가 덮이던 문제 수정
- 실제 모델·Provider·timeout stage를 진단 문자열 앞부분에 기록
- Slack 사용자 조회 실패 시 시스템 이벤트를 사람 요청으로 승격하지 않음

관련 specialist·orchestration·Slack 테스트 144건과 Ruff를 통과했다. 전체 테스트는
Windows에서 WSL이 설치되지 않아 bash 기반 specialist runtime 테스트 30건이 환경 오류로
실패했고, 나머지 2,396건은 통과했다. Linux 배포 환경에서 전체 테스트와 운영 smoke
test를 다시 실행해야 한다.
