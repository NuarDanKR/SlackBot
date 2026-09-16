# Hermes timeout 근본 수정 · 근거 보기 UX — 구현 기록

> 작성: 2026-09-16 (Claude)
> 인계: [`2026-09-16-hermes-timeout-root-fix-handoff.md`](2026-09-16-hermes-timeout-root-fix-handoff.md)
> 선행: [`2026-09-16-hermes-timeout-empty-output-implementation.md`](2026-09-16-hermes-timeout-empty-output-implementation.md)
> 상태: 구현 완료 · **운영 검증 대기**

## 1. 변경 파일

| 파일 | 무엇을 |
|---|---|
| `src/tybot/specialist_contract.py` | 단계 이름 재정의(`discovery`/`finalize`/`recovery`/`total`), 180초 예산, 절대 경계, `expired` 의미 수정 |
| `src/tybot/specialist_adapters.py` | seed-first, 근거 중복 제거, 검색 라운드 2회, 단계 전환, 토큰 상한 4,096 |
| `src/tybot/specialist_router.py` | 단계 timeout 을 복구 가능으로, 런타임 진단값 확장 |
| `src/tybot/evidence_view.py` | 버튼 표시 조건, `record_id` value, `modal()`·`fallback_blocks()`·`stored_report()` |
| `src/tybot/audit.py` | `QALog.by_record_id()` — 본인 기록만 |
| `src/tybot/slack/pilot.py` | 저장된 ref 재개방, `views_open` 모달, ephemeral 폴백 닫기 |
| `tests/test_specialist_deadline.py` | 38건 |
| `tests/test_evidence_view.py` | 30건(재검색 전제 폐기) |
| `tests/test_specialist_contract.py`·`test_tool_specialist.py` | 단계 이름·검색 상한 반영 |

**DB 스키마 변경 없음. 배포 후 추가 명령 없음.**

## 2. 적용한 시간 예산

```python
SpecialistDeadlines(
    total=180.0, discovery=100.0, finalize=45.0,
    recovery=20.0, delivery_reserve=15.0, per_call=45.0,
)
```

합계 계약 `100 + 45 + 20 + 15 = 180` 을 **생성 시점에 검증**한다. 남으면 아무도 안
쓰는 시간이고, 모자라면 뒤 단계가 시작도 못 한다.

경계는 **절대값**이다(`discovery_ends=100`, `finalize_ends=145`, `hard_ends=165`).
단계가 바뀔 때 45초를 새로 더하지 않는다 — 그러면 단계 수만큼 시간이 늘어난다.

### 왜 늘렸나

운영 13:03 에 **77.3초로 성공**한 질문이 14:23·14:31 에 74.5~76.2초로 실패했다.
75초 예산이 그 질문이 원래 쓰던 시간보다 **짧았다** — 고친 것이 아니라 회귀였다.

늘리기만 하면 같은 광범위 검색이 더 오래 돌 뿐이다. 그래서 §3(시간)과 §4(범위)를
함께 넣었다.

## 3. seed-first 와 중복 제거

### seed-first

마스터가 이미 권한 필터를 통과시켜 준 `request.evidence` 를 프롬프트 **맨 앞**에
놓고, 그 뒤에 이렇게 적는다.

```text
위 근거는 이미 권한을 확인해 고른 것입니다. **먼저 이것으로 답할 수 있는지
판단하세요.** 충분하면 검색하지 말고 바로 답합니다.
모자랄 때만 검색하고, 그때도 질문에 나온 현장명·문서명·기간을 검색어에
유지하세요. 범위를 넓히면 다른 현장 자료가 답에 섞입니다.
```

**Hermes 원본 계약을 고치지 않았다.** 우리 governed prompt 와 도구 제공 조건에서
해결한다 — 받은 계약은 받은 자리에 둔다.

### 중복 제거

`[첨부본문:손익.xlsx]` 와 `[첨부추출:손익.xlsx]` 는 같은 파일이다. 표식이 있으면
그것으로, 없으면 정규화한 본문으로 한 후보만 남긴다. **자르거나 고치지 않는다** —
같은 것을 두 번 싣지 않을 뿐이다.

두 벌이 실리면 모델은 자료가 두 배로 있다고 읽고 출처도 두 줄이 된다.

### 검색 라운드

새 검색을 시작할 수 있는 라운드는 **2회**다. 그 뒤 라운드에는 검색 도구를 아예
주지 않고(`_read_only`), 그래도 부르면 **실행하지 않는다.** 주지 않는 것과
거절하는 것을 함께 둬야 상한이 실제로 상한이다.

읽기(`read_document`·`read_channel`)는 계속 허용한다 — 찾은 것을 읽어야 답을 쓴다.

## 4. 단계 전환과 복구

```text
discovery 100초 경과  →  새 검색 안 함, finalize 로 전환  (예외 아님)
finalize 45초 소진    →  recovery 20초 한 번
hard_ends(165초) 경과 →  StageTimeout(total). 요청 종료
```

`_finalize()` 진입 **전에** `adapter.phase = "finalize"` 를 세운다. 이 줄이 없으면
최종화 중 timeout 이 `discovery` 로 기록되고, 콘솔에서 탐색이 느린 것처럼 보인다.

`RECOVERABLE` 에 단계 timeout 을 넣었다.

```text
invalid-output:empty            빈 출력
invalid-output:max-tokens       토큰 상한에서 끊김
specialist-timeout:discovery    탐색에서 끝남 — 읽은 것이 있으면 마무리 가능
specialist-timeout:finalize     최종화에서 끝남
```

`deadline.aborted` **하나로 막지 않는다**(인계 §3.3). 탐색에서 시간이 끝난 것은
「그만 찾으라」 지 「요청 종료」 가 아니다.

복구하지 않는 것은 그대로다 — 출처 포함·길이 초과(명시적 계약 위반), 인증·권한·
모델 미등록, 근거 0건, 남은 시간 없음.

## 5. 구현 중 테스트가 잡은 실제 결함 2건

1. **`require_time()` 이 전체 종료를 표시하지 않았다.** `expired` 면 바로 raise 해서
   `aborted=False` 로 남았고, 늦게 온 결과를 버리는 검사가 그 표식을 보는데 서
   있지 않았다. 표식을 먼저 세우고 raise 하도록 고쳤다. `expired` 도
   `aborted or 남은 시간 없음` 으로 넓혔다 — 바깥에서 닫은 요청이 안쪽에서 계속
   돌던 구멍이다.
2. **토큰 상한 분리가 무의미했다** — §6 참조.

## 6. 인계에서 **하지 않은 것** 하나 — 토큰 분리

인계 §4.3 은 "도구 선택 호출과 최종 답변 호출의 `max_tokens` 를 분리하고 도구
선택은 2,048~4,096" 을 요구했다. **분리하지 않았다.**

이유: seed-first 를 넣은 뒤로 **첫 라운드가 곧 최종 답변일 수 있다.** 도구를 줬다고
그 호출이 답을 안 쓰는 것이 아니다. 도구 쪽만 낮추면 seed 로 바로 답하는 경로가
토큰 상한에서 끊기고, 그건 방금 고친 `invalid-output:max-tokens` 와 같은 모양이다.
thinking 토큰이 같은 예산을 쓰는 것(Opus 5 는 끌 수도 없다)이 위험을 더 키운다.

처음에는 3,072/4,096 으로 나눴다. 테스트가 "두 값이 실제로 달라야 의미가 있다" 를
물었고, 그 순간 위 위험이 드러났다.

**모든 전문 봇 호출이 4,096** 을 쓴다(8,192 에서 내림). 인계가 막으려던 것
(8,192 를 모든 곳에 그대로 주는 것)은 지켰다. 운영에서 잘린 답이 관측되지 않고
도구 선택 호출이 실제로 짧다는 근거가 생기면 그때 나눈다.

## 7. `근거 보기`

### 버튼 표시 조건(§6.1)

```python
blocks(body, record_id=..., has_evidence=bool(ans.evidence_refs),
       answered=ans.reason in ("answered", "advice"))
```

셋이 모두 참일 때만 붙는다. `timeout`·`specialist_unavailable`·근거 없음에는
붙지 않는다. **검색어가 있다는 이유로 만들지 않는다.**

### 재검색 폐기(§6.2)

버튼 value 는 `qa_record_id` 하나다. 클릭하면:

1. `QALog.by_record_id(workspace, id, user=클릭한 사람)` — **본인 기록만.**
   버튼 값이 새어도 남의 답변 근거는 나오지 않는다.
2. `refs_from_json(row["evidence_refs"])`
3. `store.resolve_refs(refs, 지금 권한 ctx)` — 권한이 사라진 ref 는 안 나온다.

`_evidence_lines()`(옛 재검색 경로)는 **지웠다.** 남겨 두면 다음 사람이 "이미
있으니 쓰자" 하고 같은 결함을 되살린다.

본문 끝에 `_지금 권한으로 다시 연 것입니다. 새로 검색하지 않았습니다._` 를 적는다.
열지 못한 ref 는 사유를 고정 낱말로 밝힌다(`권한이 바뀌어 볼 수 없습니다` 등).

### 모달(§6.3)

`views_open` 으로 연다. 제목 `답변 근거`, 닫기는 **Slack 기본 버튼**.
닫으면 원래 스레드가 그대로 남는다.

`trigger_id` 가 없거나 모달 생성이 실패할 때만 ephemeral 폴백이고, 거기에는
`닫기` 버튼(`respond(delete_original=True)`)을 준다.

## 8. 관측값

`specialist_call.routing_reason` 에 본문 없이:

```text
phase=  model=  provider=  stop_reason=  rounds=
seed=  search_rounds=  opened=  in_tok=  out_tok=
by_tool=search=2,read_document=3
timeout_stage=  deadline_total_ms=  specialist_elapsed_ms=  output_chars=  recovered=1
```

`opened` 는 **실제로 본문을 연** 문서 수다 — 검색 결과에 나왔다고 세지 않는다.
`seed`·`search_rounds`·`opened` 셋이 같이 있어야 "시간만 늘리고 검색은 그대로" 를
판별할 수 있다.

`runtime_meta jsonb` 스키마는 아직 안 만들었다. 인계가 허용한 문자열 경로를 썼고,
다음에 스키마를 손댈 때 옮긴다.

## 9. 테스트

```text
pytest                        2448 passed
ruff check src tests scripts  All checks passed
```

실제 sleep 없이 주입 시계와 가짜 Provider 로 돈다.

### 되돌리기 실험 — 12건 전부 잡힘

| 되돌린 것 | 결과 |
|---|---|
| 총 시간을 75초로 되돌림 | 3 errors ✅ |
| 단계 합 검증 제거 | 1 failed ✅ |
| `abort` 표식을 `expired` 에서 뺌 | 1 failed ✅ |
| 전체 종료 시 표식 안 세움 | 1 failed ✅ |
| 최종화 전 `phase` 설정 제거 | 1 failed ✅ |
| 검색 라운드 상한 제거 | 1 failed ✅ |
| 상한 뒤 검색 실행 거절 제거 | 2 failed ✅ |
| 근거 중복 제거 제거 | 1 failed ✅ |
| seed-first 안내 제거 | 1 failed ✅ |
| 토큰 상한을 8,192 로 되돌림 | 1 failed ✅ |
| 실패 답변에도 버튼 | 1 failed ✅ |
| (선행 작업 11건은 그대로 유지) | |

**중복 제거와 토큰 상한은 처음에 안 잡혔다.** 함수만 직접 부르는 테스트였기
때문이다 — 어댑터를 지나도록 고쳤다. 같은 종류의 구멍이 선행 작업에서도 두 번
나왔다.

## 10. 아직 운영에서만 확인할 수 있는 것

인계 §8 의 항목은 실제 Slack·Provider 가 필요해 돌리지 못했다.

- [ ] 부산항 신항 웅동지구 질문 3회 중 timeout 0건
- [ ] 전체 처리 180초 미만, 단계별 시간이 콘솔에 합리적으로 표시
- [ ] 관련 파일만 근거로 쓰이고 중복 출처 없음
- [ ] timeout 후 추가 `llm_call`·tool call 0건
- [ ] 실패 답변에 `근거 보기` 버튼 없음
- [ ] 성공 답변의 모달을 닫으면 원래 스레드로 즉시 복귀
- [ ] 강제 Provider 지연에서 discovery/finalize/recovery timeout 각각 관측

**성공률만 오르고 p90·비용이 크게 늘면 완료가 아니다.** 검색 라운드를 2회로
줄였으므로 `search_rounds` 와 `by_tool=search=` 는 내려가는 쪽이 정상이다.
그렇지 않으면 seed-first 가 실제로는 안 먹은 것이다.

## 11. 배포

DB 변경 없음. 새 환경변수 없음. `update.sh` + 재시작이면 된다.

배포 전 확인은 선행 기록과 같다 — `specialist_bot.execution_mode` 가 `hermes` 에
대해 `tools` 여야 복구가 동작한다.

## 12. Codex 후속 QA 보완 (2026-09-16)

최초 구현을 코드 경로 기준으로 다시 검증하면서 아래 결함을 수정했다.

1. **실제 근거 좌표 누락**
   - 기존 구현은 Hermes가 추가 검색으로 실제 읽은 줄이 아니라 마스터의 최초 후보만
     `evidence_refs`에 남길 수 있었다.
   - 도구 출력에 실제 포함된 아카이브 줄과 실시간 Slack 메시지 좌표를 `Touched`에
     기록하고, 최종 답변에는 이 좌표를 우선 저장한다.
   - 도구 출력의 30,000자 절단 뒤에 있는 줄은 모델이 보지 못했으므로 근거로 남기지
     않는다.

2. **seed-first 강제 부족**
   - 안내 문구만으로는 모델이 첫 회차부터 전체 검색을 호출할 수 있었다.
   - seed가 있으면 첫 회차에 실제 `search`를 노출하지 않고 `request_search`만 제공한다.
     부족함을 명시한 호출만 실제 검색으로 변환한다.
   - 이번 회차에 제공하지 않은 도구 이름을 모델이 반환해도 실행하지 않는다.
   - 검색은 라운드뿐 아니라 실제 호출 수도 최대 2회로 제한한다.

3. **동명 파일 오인 중복 제거**
   - 파일명만 같은 월별 보고서와 재업로드를 합치지 않는다.
   - 수집 경로 표식을 정규화한 뒤 본문까지 같은 경우만 중복으로 제거한다.

4. **근거 보기 권한과 수명**
   - 채널 답변은 원 질문자만이 아니라 같은 채널의 현재 구성원이 열 수 있다.
   - DM 답변은 원 질문자만 열 수 있다.
   - QA 기록은 최근 3개 파일이 아니라 전체 일자 파일을 최신순으로 찾아, 오래된 Slack
     버튼도 유효하게 했다.

5. **진단 누락**
   - 답변 이슈 내보내기에서 `specialist_unavailable`을 오류와 전문 봇 실패로 집계한다.
   - `search_calls`를 실행 추적에 추가했다.

### 검증 결과

```text
핵심 회귀 테스트                         224 passed
전체 회귀(Windows Bash 전용 2파일 제외)  2378 passed, 8 warnings
ruff check src tests scripts              All checks passed
git diff --check                          통과
```

경고 8건은 Windows Git 훅 테스트가 로컬 콘솔 출력을 UTF-8로 읽을 때 발생하는 기존
인코딩 경고다. 제외한 `test_specialist_runtime_store.py`,
`test_specialist_runtime_unit.py`는 이 Windows 환경에 없는 Bash/WSL 실행을 요구한다.

### 배포 후 반드시 볼 값

- `phase`, `search_rounds`, `search_calls`, `opened`, `specialist_elapsed_ms`
- 첫 seed가 있는 요청에서 첫 회차의 실제 `search` 실행이 0인지
- 동일 질문 3회에서 `specialist-timeout:*`, `invalid-output:*`가 재발하지 않는지
- 성공 답변의 `근거 보기`가 Hermes가 실제 읽은 문서 줄을 여는지
