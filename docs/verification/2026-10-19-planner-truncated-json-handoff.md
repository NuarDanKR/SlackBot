# TYIT 분류기 불완전 JSON — 수정 내용과 Codex 인계

Written for: Codex. 작성: Claude, 2026-10-19. **커밋하지 않았습니다.**

## 1. 무엇이 났나

2026-10-19 08:19 · tyit `C0C9A86JUSJ` · 긴 비교 질문.

`intent.plan()` 의 응답이 **JSON 으로 시작해 `kind=search` 까지 적다가 끊겼고**,
`_extract_json()` 은 닫는 괄호가 없자 이렇게 보고했다.

```
JSON 없음: '{"tasks": [{"kind": "search", "question": "…'
```

원인은 둘이 겹친 것이다.

### (1) 출력 상한 500 이 지금 스키마에 맞지 않는다

한 task 가 쓰는 칸이 **열다섯**이고 그중 셋(`question` · `standalone_question` ·
`research_question`)은 **질문 문장을 통째로** 담는다. 프롬프트가 그렇게 시킨다 —
「풀 것이 없으면 원문 조각 그대로 둔다」, 「뺄 지시가 없으면 같은 문장」.

한국어는 글자당 토큰이 거의 1 이다. 200자짜리 질문 하나면 그 셋만으로 500 을
넘는다. task 는 최대 3개까지 허용하므로 실제 필요량은 그 몇 배다.

### (2) 잘림과 틀림이 같은 오류였다

```python
start, end = s.find("{"), s.rfind("}")
if start == -1 or end <= start:
    raise ValueError(f"JSON 없음: {raw[:120]!r}")
```

끊긴 출력에는 `{` 는 있고 `}` 가 없다 → `rfind` 가 `-1` → **「JSON 없음」**.
모델이 엉뚱한 산문을 쓴 것과 분량이 모자라 끊긴 것이 **같은 문장으로** 나왔고,
그래서 상한이 원인이라는 신호가 로그 어디에도 없었다.

사람이 할 일이 전혀 다르다 — 잘림은 상한을 올리면 되고, 스키마 오류는 프롬프트를
고쳐야 한다.

## 2. 고친 것

### (A) 상한을 스키마에 맞춘다

```python
PLANNER_MAX_TOKENS = 1200          # 500 → 1200
PLANNER_RETRY_MAX_TOKENS = 2400    # 잘렸을 때 한 번만
```

둘 다 저가 분류 모델(`claude-haiku-4-5`) 기준이라 재시도 한 번의 비용이 답변
한 건의 비용을 넘지 않는다.

### (B) 질문 반복·빈 칸 출력을 프롬프트에서 끊는다

상한만 올리면 같은 문제가 더 긴 질문에서 다시 난다. **출력 자체를 줄인다.**

| 칸 | 전 | 후 |
|---|---|---|
| `standalone_question` | 풀 것이 없어도 원문 조각을 **그대로 다시** 적음 | `question` 과 **다를 때만** |
| `research_question` | 뺄 지시가 없어도 같은 문장 | 뺄 산출물 지시가 **있을 때만** |
| `reference_mode`·`asks_about_our_sources`·`artifact_*`·`target_unit` | 기본값도 항상 출력 | 기본값이면 **칸 자체를 생략** |
| `days` | 예시에 있음 | **뺐다** — 코드가 `parse_period(text)` 로 정하므로 읽지도 않는다 |

생략해도 안전한 이유는 **`master_planner` 가 이미 그 기본값을 쓰고 있기
때문**이다(`artifact_for()` 의 `not in DELIVERIES → message` 등). 새 기본값을
만든 것이 아니라 모델에게 **적지 말라고** 한 것뿐이다.

> **`confidence` 는 생략 목록에 넣지 않았다.** 빼면 `0.0` 이 되고,
> `specialist_router.select()` 의 `confidence < min_confidence` 에 걸려 **전문 봇
> 라우팅이 통째로 `CLARIFY` 로 죽는다.** 그래서 「`specialist` 를 고르면
> `capability` 와 `confidence` 도 반드시 넣는다」 를 프롬프트에 못박았다.

`standalone_question` 이 생략되면 **코드가 `question` 으로 채운다.** 비워 두면
`master_planner._standalone()` 이 이전 턴 질문을 앞에 붙이는 다른 가지로 가서,
묻지 않은 주제가 검색어에 섞인다. 생략은 「같다」 는 뜻이지 「없다」 가 아니다.

### (C) 잘림과 틀림을 가른다

```python
class PlannerOutputError(ValueError):      code = "planner_invalid_json"
class PlannerOutputTruncated(…Error):      code = "planner_truncated"
```

판정 순서:

1. `stop_reason == "max_tokens"` — **프로바이더가 직접 말한 것**이라 가장 확실하다
2. `{` 는 있는데 닫히지 않았다 — `_is_incomplete_json()` 이 문자열 안의 괄호를
   빼고 깊이를 센다(2026-10-19 장애가 정확히 이 모양)
3. 그 외 `JSONDecodeError` — 틀린 출력

**끊긴 JSON 을 닫지 않는다.** 닫으면 모델이 쓰다 만 task 가 완성된 판단처럼
흘러가고, 빠진 칸은 기본값으로 채워져 아무 신호도 안 남는다.

### (D) 진단 로그 — 계기판만, 본문은 없다

```
분해 실패 code=planner_truncated model=claude-haiku-4-5-… stop=max_tokens
          in=1842 out=1200 chars=4731 unclosed=True
```

**본문을 찍던 줄을 지웠다.** 전에는 `raw=%r` 로 응답 앞 200자를 남겼는데, 분해
응답의 `question` 칸에는 사용자가 쓴 문장이 그대로 들어 있다 — 그 로그는 업무
내용을 journal 로 복사하는 것과 같았다(원칙 5). `_extract_json` 의 예외 메시지에
있던 `raw[:120]` 도 같이 지웠다.

길이와 괄호 균형(`chars` · `unclosed`)은 남긴다. 내용이 아니라 모양이고,
「상한에서 끊겼나」 를 로그만 보고 되짚을 수 있는 값이다.

### (E) 재시도 — 잘림에만, 한 번만

`_planner_json()` 이 맡는다.

- **잘렸을 때만.** 스키마가 틀린 답은 다시 물어도 같은 답이 온다 — 비용과
  시간만 쓰고 결과를 바꾸지 못한다
- **한 번만.** 두 번째 잘림은 상한 문제가 아니라 프롬프트나 질문 쪽 문제다.
  계속 올리면 한 질문이 분류 예산을 통째로 먹는다
- **같은 모델로.** `resp.model` 을 그대로 쓴다. 재시도가 더 비싼 모델로 번지지
  않는다
- 재시도 자체가 막히면(비용 상한·모델 장애) **첫 실패를 올린다.** 사람이 봐야
  할 것은 「잘렸다」 이지 「재시도가 안 됐다」 가 아니다
- 돌려주는 `resp` 는 **실제로 읽은 응답**이다. 진단 로그가 첫 호출의 토큰 수를
  적으면 상한을 올린 뒤에도 옛 숫자가 남아 원인을 거꾸로 짚게 된다

### (F) OpenAI 프로바이더가 `stop_reason` 을 안 채우고 있었다

`finish_reason == "length"` 를 `stop_reason = "max_tokens"` 로 정규화했다.
안 하면 그쪽 설치에서는 1번 신호가 없어 2번(구조 판정)에만 기댄다.

## 2b. Codex 검수 반영 (2026-10-19)

세 건 전부 맞는 지적이었다.

### P1 — 상한 종료 응답을 정상으로 수락했다

`stop_reason == "max_tokens"` 인데 JSON 문법이 완성돼 있으면 그대로 반환했다.
`truncated` 를 「괄호가 없을 때」 와 「파싱이 깨졌을 때」 에만 썼기 때문이다.

**종료 사유를 형식 판정보다 앞에 둔다.** 괄호가 맞아도 잘림으로 본다.

```python
if stop_reason == "max_tokens":
    raise PlannerOutputTruncated("상한에서 끊긴 응답")
```

이유: `max_tokens` 는 **모델이 스스로 멈춘 것이 아니라는 뜻**이다. 세 개를
쓰려다 첫 개를 닫은 자리에서 끊겼는지, 다 쓰고 뒤에 설명을 붙이다 끊겼는지는
본문 모양으로 구별할 수 없다. 구별할 수 없는 것을 추측해 수락하면 **반쪽 계획이
완성된 판단처럼** 흘러간다.

값은 호출 하나다. 다 쓰고 끊긴 경우에도 재시도가 같은 답을 더 넉넉한 상한으로
돌려준다 — 잃는 것은 호출 하나, 막는 것은 모르는 채로 내리는 판단이다.

구조 판정(`_is_incomplete_json`)은 그대로 둔다. `stop_reason` 을 안 주는
프로바이더에서는 그것이 유일한 신호다.

### P2 — 재시도 실패 로그에 첫 응답이 찍혔다

`raw, resp = _planner_json(...)` 에서 두 번째 응답의 파싱이 실패하면 **대입이
완료되지 않아** 바깥 `resp` 는 첫 응답이다. 두 번째가 `end_turn`·2,400 토큰인데
로그에는 첫 번째 `max_tokens`·1,200 이 찍혔다 — 상한을 올린 뒤에도 옛 숫자가
남아 사람이 「상한이 아직 모자라다」 로 거꾸로 짚는다.

읽은 응답을 **예외에 매단다.**

```python
except PlannerOutputError as second:
    second.response = retried
    raise
...
_log_planner_failure(e, getattr(e, "response", None) or resp)
```

### P3 — 재시도에 시간 예산이 없었다

비용은 gateway 가 보지만 **시간은 별개다.** 첫 호출도 재시도도
`timeout_seconds` 를 안 넘겼다.

```python
PLANNER_DEADLINE_SECONDS = 25.0    # 분해 전체
PLANNER_RETRY_MIN_SECONDS = 6.0    # 이보다 적게 남으면 재시도하지 않는다
```

시계를 **한 번만** 만들어 모델 폴백 후보와 잘림 재시도가 같은 절대 경계를 본다.
후보마다 새 시간을 주면 후보 수만큼 느려진다. 분해는 답변의 앞단이라 여기서 쓴
시간이 전문 봇 예산에서 그대로 빠진다.

2초 남은 상태로 2,400 토큰을 부르면 어차피 시간 초과로 끝난다. 그 2초는
「잘렸다」 를 돌려주는 데 쓰는 편이 낫다.

> **부작용 하나.** 첫 호출에도 timeout 이 생겼다. 전에는 무제한이었다.
> 25초는 저가 분류 모델의 1,200 토큰 호출에 넉넉하지만, **프로바이더가 느린
> 순간에 전에는 되던 호출이 끊길 수 있다.** 그때는 기존 `except Exception`
> 경로로 가므로 규칙 폴백(또는 Master v2 는 `PlanningUnavailable`)이다.

### 시험 대역도 같이 고쳤다

`Provider` 프로토콜에는 `timeout_seconds` 가 있는데 `test_intent.py` 의 대역
둘이 안 받고 있었다(`TypeError`). 받게 하고 **값을 기록**하게 해서, 그 기록이
P3 의 회귀 시험이 된다.

### 「이번 질문」 회귀 시험 — 지적대로 다른 질문이었다

`LONG_COMPARE` 는 발표 비교 질문이다. 실제는 **토목현장 비교 질문**이다.
`SITE_COMPARE`(현장 둘 × 공사기간·기성·공정률·설계변경 비교)를 더했다.

**사내 질문 원문은 저장소에 넣지 않는다**(원칙 5). 같은 성격의 문장으로 세웠다 —
분해 출력 분량을 정하는 것은 길이와 비교 축의 수이고, 그 둘을 맞췄다.
**원문 그대로 고정이 필요하면 질문 텍스트를 주면 바꾼다**(§5 참조).

## 3. 변경 파일

| 파일 | 내용 |
|---|---|
| `src/tybot/intent.py` | 상한 상수 2개 · `PlannerOutputError`/`…Truncated` · `_is_incomplete_json()` · `_extract_json(stop_reason=)` · `_stop_reason()` · `_log_planner_failure()` · `_planner_json()` · `PLANNER_PROMPT` 출력 규칙 · `standalone_question` 폴백 |
| `src/tybot/gateway/providers/openai_provider.py` | `finish_reason` → `stop_reason` 정규화 |
| `tests/test_intent.py` | **신규 20건** · 대역 둘에 `timeout_seconds` 추가 |

### 함수 계약 변경

- `_extract_json(raw, *, stop_reason="")` — 인자 추가(기본값 있음). `ValueError`
  대신 **그 하위 클래스**를 던지므로 기존 `except ValueError` 는 그대로 잡힌다
- `PLANNER_MAX_TOKENS` · `PLANNER_RETRY_MAX_TOKENS` · `PLANNER_DEADLINE_SECONDS` ·
  `PLANNER_RETRY_MIN_SECONDS` · `PlannerOutputError` · `PlannerOutputTruncated` 공개
- `plan()` 이 `router.complete(timeout_seconds=…)` 를 넘긴다. **전에는 무제한이었다** —
  `Provider` 프로토콜에 이미 있는 인자라 실제 프로바이더는 그대로 받지만,
  그 인자를 안 받는 **시험 대역은 고쳐야 한다**
- `Intent.standalone_question` — 분해기가 생략하면 **`question` 으로 채워진다**
  (전에는 모델이 늘 채웠다). 빈 문자열로 오던 경로가 없어진다

## 4. 시험 결과

개발 PC(Windows)에서 돌렸다. 서버 절차가 아니라 결과 보고다.

| 무엇 | 결과 |
|---|---|
| `ruff check` (`src` · `tests` · `scripts`) | All checks passed |
| 전체 (`test_external_convert.py` 제외) | **5,077 passed · 31 skipped** (167s) |
| `tests/test_intent.py` 단독 | 109 passed |

### 요구된 회귀 시험

| 요구 | 시험 |
|---|---|
| 이번 긴 비교 질문 | `test_a_truncated_plan_is_retried_once_with_a_bigger_cap` — 운영에서 난 잘린 본문 그대로 |
| 잘린 JSON | `test_a_cut_off_json_is_not_reported_as_no_json` · `test_the_provider_stop_reason_decides_even_without_a_brace` · `test_a_twice_truncated_plan_gives_up_instead_of_closing_the_json` |
| 정상 JSON | `test_a_good_plan_is_not_retried` (재시도 0회) |
| API 실패 | `test_an_api_failure_still_falls_back_by_rule_when_allowed` · `test_the_retry_budget_failure_reports_the_truncation` |
| Master v2 정규식 폴백 금지 | `test_master_v2_never_falls_back_to_the_regex_planner` — API 실패·잘림·산문 **세 경우 모두** `PlanningUnavailable` |
| 로그에 본문 없음 | `test_the_diagnostic_log_carries_no_question_or_answer_body` |
| 재시도 안 할 것 | `test_a_schema_error_is_not_retried` |
| 프롬프트·상한 계약 | `test_the_output_cap_fits_the_current_schema` · `test_the_planner_prompt_asks_for_fewer_repeated_fields` · `test_an_omitted_standalone_question_falls_back_to_the_question` |
| 응답 객체 호환 | `test_a_response_without_a_stop_reason_still_plans` |
| **P1** 상한 종료는 괄호가 맞아도 잘림 | `test_a_max_tokens_stop_is_truncated_even_when_the_json_closes` · `test_a_closed_json_cut_at_the_cap_is_retried_not_accepted` |
| **P2** 로그가 실제로 읽은 응답을 찍는다 | `test_the_failure_log_reports_the_response_it_actually_read` |
| **P3** 두 호출이 한 시계를 쓴다 | `test_both_calls_share_one_time_budget` · `test_the_retry_is_skipped_when_the_budget_is_spent` |
| 실제 질문 모양(토목현장 비교) | `test_the_site_comparison_question_survives_a_truncated_first_try` |

### 시험이 실제로 빨간지 확인했다

잘림 판정을 들어내고 옛 동작(전부 같은 오류)으로 되돌려 돌렸다 → **6건 red**.
Codex 검수 반영분도 같은 방식으로 셋을 전부 되돌려 확인했다 → **6건 red**
(P1 3건 · P2 1건 · P3 2건). 복구 → 109건 green.

마지막 항목은 구현 중 실제로 깨진 것을 잡아서 넣었다. 처음에 `resp.stop_reason`
을 속성으로 직접 읽었더니 그 속성을 안 들고 오는 응답 대역에서 AttributeError 가
났고, `plan()` 의 넓은 `except Exception` 에 걸려 **분해 전체가 조용히 규칙
폴백으로 떨어졌다**(`tests/test_plan.py` 5건 red). `getattr` 로 바꿨다.

## 5. 미검증 사항

0. **토목현장 비교 질문의 원문.** `SITE_COMPARE` 는 같은 성격으로 세운 문장이고
   2026-10-19 08:19 에 실제로 들어온 문장이 아니다. 원문 그대로 고정이 필요하면
   질문 텍스트를 주면 바꾼다 — 다만 **저장소에 사내 질문 원문을 넣는 것**이
   원칙 5 와 부딪히므로, 길이와 비교 축만 맞추는 지금 모양이 맞다고 본다.
   판단이 필요한 자리다.
1. **첫 호출의 25초 timeout.** 전에는 무제한이었다. 저가 분류 모델의 1,200 토큰
   호출에 넉넉한 값이지만 **프로바이더가 느린 순간에 전에는 되던 호출이 끊길 수
   있다.** 운영에서 `분해 호출 실패(ProviderTimeout)` 가 나는지 봐야 한다
2. **실제 운영 재현.** tyit `C0C9A86JUSJ` 에서 같은 질문을 다시 던져 `search`
   task 가 나오는지. 시험은 잘린 본문을 **대역으로** 넣어 돌렸고, 실제 모델이
   1200 토큰 안에 끝내는지는 확인 못 했다
3. **상한 1200 이 충분한가.** 프롬프트 변경으로 출력이 줄 것을 전제로 잡은
   값이다. 모델이 생략 지시를 얼마나 따르는지는 **실측이 필요하다** — 운영에서
   `분해 잘림 재시도` 로그가 얼마나 나는지가 그 답이다. 자주 나면 1200 을 올리기
   전에 **프롬프트가 안 먹히는 것**부터 본다
4. **생략 지시의 부작용.** `confidence` 는 막아 뒀지만, 모델이 지시를 넓게 읽어
   `capability` 나 `terms` 까지 생략하면 라우팅이 달라진다. `capability` 빈 값의
   영향은 코드로 확인했으나(= 전문 봇 후보 없음 → 마스터) **실제 분포는 못 봤다**
5. **OpenAI 경로.** `finish_reason` 정규화는 코드로만 확인했다. 그쪽 프로바이더로
   분해를 돌리는 설치가 지금 있는지 모른다
6. **비용 영향.** 입력 토큰은 그대로이고 출력 상한만 올렸다. 상한은 **최대치**라
   평상시 요금은 거의 안 변하지만, 재시도가 잦으면 분류 비용이 올라간다.
   `health` 의 비용 추이로 보는 쪽을 권한다

## 6. 안 건드린 것

- `classify()` 의 `max_tokens=200` — 그쪽 스키마는 `{"kind","days","terms"}` 뿐이라
  잘릴 분량이 아니다. 같이 올리면 안 쓰는 예산만 잡는다
- `plan()` 의 모델 폴백 루프(`CLASSIFIER_MODEL → None`)와 `allow_rule_fallback`
  의미 — 그대로다
- 채널·ACL 범위 — 분해기는 권한을 보지 않는다. 이번 변경이 닿지 않는다
- 커밋·push
