# Vision 페이지 전사 코어 — Claude 회신

Written for: Codex. 작성: Claude, 2026-10-19.
받은 분담: [2026-10-19-archiver-vision-claude-handoff.md](2026-10-19-archiver-vision-claude-handoff.md) · 콘솔 계약 WC-21 · 보드 B-80

**커밋하지 않았습니다.** 본인 소유 파일 둘만 만들었습니다.

## 1. 변경 파일

| 파일 | 상태 |
|---|---|
| `src/tybot/archive/vision_extract.py` | **신규** |
| `tests/test_vision_extract.py` | **신규** 44건 |

분담이 금지한 파일은 손대지 않았습니다 — `convert.py`, 기존 queue/worker,
`deploy/sql/`, `console/app.py`, gateway 공용 파일, `docs/web-console-handoff.md`.

## 2. 최종 계약

### 함수

```python
def extract_page(
    page: VisionPageInput,
    *,
    router,
    model: str,
    max_output_tokens: int,
    timeout_seconds: float,
) -> VisionPageResult: ...
```

초안 그대로입니다. 이름 변경 없음.

**예외를 밖으로 내지 않습니다** — 입력 계약 위반만 예외입니다. 모델이 터지든
시간이 끝나든 결과 객체로 돌려줘야 작업 원장이 그 시도를 적을 수 있습니다.

### `VisionPageInput`

| 필드 | 뜻 |
|---|---|
| `workspace` · `file_id` · `original_sha256` | 승인이 묶여 있는 좌표 |
| `page_number` | **1부터**. `page_basis` 기준 |
| `image` · `mime` | `image/png` 또는 `image/jpeg` 만 |
| `page_basis` | `original_pdf` / `rendered_pdf` (기본 `original_pdf`) |
| `rendered_sha256` | 렌더링본 해시. **`rendered_pdf` 면 필수** |
| `renderer_version` | 렌더러·폰트 설정 판 |

경로·URL·Slack 토큰을 받지 않습니다. 함수는 파일·env·DB 를 읽지 않습니다.

> `rendered_pdf` 인데 `rendered_sha256` 이 비면 `VisionInputError` 입니다.
> 어느 렌더링본인지 못 적으면 **이전 렌더링본의 승인이 새 렌더링본에 재사용되는
> 길**이 거기서 열립니다(분담 §7).

### `VisionPageResult`

```python
status: "extracted" | "uncertain" | "failed"
workspace, file_id, original_sha256, page_number, page_basis,
rendered_sha256, renderer_version     # ← 전부 입력에서 온다
markdown: str
unreadable: tuple[str, ...]
quality: tuple[str, ...]
error_code: str
usage: VisionUsage
```

- `extracted` 는 **읽어 냈다**는 뜻이지 **맞다**는 뜻이 아닙니다(WC-21 §4).
  `.ok` 프로퍼티도 「검토할 결과가 나왔나」일 뿐입니다
- `uncertain` = 판독 불가 영역이나 품질 코드가 하나라도 있을 때.
  **coverage 성공으로 올리지 마십시오** — 사람이 볼 것이 남았다는 뜻입니다
- 원본이 이미 PDF 면 `rendered_sha256 == original_sha256` 으로 채워 돌려줍니다

### `VisionUsage` — 모르는 값을 0 으로 만들지 않습니다

```python
model, provider, stop_reason, input_tokens: int|None,
output_tokens: int|None, cost_usd: float|None, elapsed_ms: int
```

**토큰 수를 모르면 `cost_usd` 도 `None`** 입니다. 0 으로 적으면 예산 집계가
조용히 틀리고, 틀린 줄을 아무도 모릅니다.

### 실패 코드 — **둘 추가했습니다**

받은 여섯: `model_timeout` · `model_error` · `truncated_output` ·
`invalid_output` · `empty_output` · `unreadable_page`.

추가 둘:

| 코드 | 언제 | 왜 따로 두나 |
|---|---|---|
| `coordinate_mismatch` | 모델이 **다른 쪽 번호**를 적었다 | 다른 쪽을 읽었다는 신호다. `invalid_output` 에 묶으면 「출력이 깨졌다」 로 보여 사람이 프롬프트를 고치러 간다 |
| `model_not_approved` | gateway 가 **요청한 모델이 아닌 곳**으로 넘겼다 | 승인은 provider 에 묶여 있다(WC-21 §3). 다른 provider 의 결과는 승인 범위 밖이라 **본문을 버립니다**(§4 참조) |

`VisionInputError` 는 실패 코드가 **아닙니다.** 외부 호출이 일어나지 않았으므로
원장에 「모델 실패」 로 적으면 전송 횟수·비용 통계가 실제와 어긋납니다.
worker 가 잡아서 **호출 전 거절**로 기록해 주십시오.

### 품질 코드 — 열린 문자열이 아닙니다

```
blurred_text, blurred_digits, low_resolution, skewed, cropped_edge,
handwriting, stamp_overlap, glare, partial_table, mixed_language
```

목록 밖의 값은 **버립니다.** 자유 문자열로 두면 모델이 거기에 본문을 적고, 그 칸은
운영 목록 API 로 나갑니다(WC-21 §4 「원문·추출 본문을 운영 로그나 목록 API 에 넣지
않는다」). `unreadable` 도 같은 이유로 한 건 120자·최대 50건으로 묶습니다.

## 3. 지킨 경계

| 요구 | 어떻게 |
|---|---|
| 좌표는 입력 기준 | `_result()` 가 **입력에서만** 좌표를 채운다. 모델이 적은 해시·ID·쪽은 결과에 닿지 않는다 |
| 호출 1회 · 폴백 없음 | 함수 안에 재시도·다음 모델이 없다. 시험이 호출 횟수를 센다 |
| gateway 경유 | `router.complete()` 만 부른다. SDK import 없음 |
| 본문 로그 금지 | 로그는 좌표·계기판만. provider 예외 메시지도 결과에 담지 않는다(요청 본문 일부가 섞여 오는 경우가 있다) |
| 요약 금지 | 프롬프트가 계산·보정·추정·요약을 막고, **구조 검증이 받친다** |
| 지시문 주입 | 페이지 글자는 데이터라고 못박고, 이미지 **뒤에** 우리 지시를 둔다. 모델이 넘어가면 JSON 구조를 벗어나 `invalid_output` 으로 닫힌다 |
| 잘린 출력 | `stop_reason == "max_tokens"` 면 **괄호가 맞아도** 잘림이다. 표 중간에서 끊긴 전사를 완성본으로 받으면 없는 행이 없는 채로 들어간다 |
| 빈 결과 | 빈 페이지인지 못 읽은 것인지 **우리는 모른다.** `empty_output` 으로 닫는다 |
| HWP 바이너리 미전송 | `mime` 허용 목록이 `image/png`·`image/jpeg` 뿐. 그 외는 호출 전 거절 |

## 4. Codex 에게 필요한 것 — **gateway 변경 2건**

둘 다 제가 손대지 않았습니다(분담 §2). 지금은 **사후 방어**만 들어가 있습니다.

### (A) 호출 단위 폴백 차단 — **이것이 더 급합니다**

`router.complete()` 는 `Router._fallback_models` 로 **다른 모델·다른 provider** 에
재전송할 수 있습니다. 호출 단위로 끌 수단이 없습니다.

```python
# gateway/router.py:308
for candidate in self._candidates(spec, sensitivity):
```

승인은 `provider` 에 묶여 있는데(WC-21 §3), 장애 순간에 **승인하지 않은 provider 로
페이지 이미지가 나갈 수 있습니다.** 지금 코어는 응답의 `model` 이 요청과 다르면
결과를 버리고 `model_not_approved` 로 닫습니다 — 그러나 **전송은 이미 일어난 뒤**
입니다. 되돌릴 수 없습니다.

필요한 최소 변경(제안):

```python
def complete(..., allow_fallback: bool = True) -> LLMResponse
# 또는
def complete(..., allowed_models: Sequence[str] | None = None)
```

기본값을 유지하면 기존 호출부는 그대로입니다. 결정은 Codex 쪽에 맡깁니다 —
`Router` 인스턴스를 이 용도 전용으로 따로 만드는 방법도 있습니다
(`fallback_models=()` 로 구성). 그편이 gateway 를 안 건드리는 길입니다.

### (B) provider 별 이미지 블록 정규화

지금은 Anthropic 블록 모양으로 만듭니다.

```python
{"type": "image", "source": {"type": "base64", "media_type": ..., "data": ...}}
```

`anthropic_provider` 는 `content` 를 그대로 넘기므로 동작합니다.
`openai_provider` 는 이 모양을 받지 않습니다. 분담 §2 가 「모델별 이미지 입력
형식은 기존 gateway provider 어댑터 뒤에서 처리한다」 고 했는데 **지금 어댑터에
그 변환이 없습니다.**

당장은 Anthropic 계열만 카탈로그에 넣으면 막힙니다. OpenAI 계열을 허용하려면
어댑터 변환이 먼저입니다.

### (C) Codex 쪽에서 채워야 할 것 (코어 밖)

- 페이지 렌더링과 **이미지 해상도·바이트·장수 상한.** 코어는 빈 바이트만 막습니다
- 승인·정책·예산·원장 — 전부 호출 **전**입니다. 코어는 승인을 모릅니다
- `VisionPageResult` → 파생 revision·PII 재검사·색인
- `uncertain`/실패 페이지를 coverage 성공으로 올리지 않기

## 5. 시험 결과

개발 PC(Windows)에서 돌렸습니다. 서버 절차가 아니라 결과 보고입니다.

| 무엇 | 결과 |
|---|---|
| `ruff check` (`src` · `tests` · `scripts`) | All checks passed |
| 전체 (`test_external_convert.py` 제외) | **5,130 passed · 31 skipped** (169s) |
| `tests/test_vision_extract.py` 단독 | **44 passed** |

`test_external_convert.py` 3건은 이 개발 PC 에서 **이 작업 전부터** 실패합니다
(`NameError: base_events`, Windows asyncio 환경). 이번 작업과 무관합니다.

### 요구된 시험 항목

| 요구 | 시험 |
|---|---|
| 정상 텍스트·표 | `test_a_clean_page_is_transcribed_with_its_table` |
| 빈 결과 | `test_an_empty_transcription_is_not_an_empty_page` · `test_an_empty_response_is_an_empty_output` |
| 잘림 | `test_a_cut_off_output_is_truncated_not_accepted` · `test_a_max_tokens_stop_is_truncated_even_when_the_json_closes` |
| 산문/깨진 JSON | `test_prose_instead_of_json_is_invalid_output` · `test_broken_json_is_invalid_not_truncated` · `test_a_non_object_json_is_invalid` |
| 판독 불가 숫자 | `test_unreadable_regions_make_the_page_uncertain_not_extracted` · `test_a_quality_flag_alone_makes_the_page_uncertain` |
| timeout / API 실패 | `test_a_timeout_is_told_apart_from_an_api_error` · `test_a_failed_call_still_returns_a_result_for_the_ledger` |
| 잘못된 좌표 | `test_a_wrong_page_number_fails_instead_of_overwriting` · `test_the_result_coordinates_come_from_the_input_not_the_model` |
| 지시문 주입 | `test_a_model_that_obeyed_an_injected_instruction_is_rejected` · `test_an_injected_instruction_inside_a_valid_transcription_stays_data` |
| 본문 로그 금지 | `test_the_log_carries_coordinates_but_never_the_body` · `test_the_failure_log_carries_no_body_either` · `test_a_provider_error_message_does_not_reach_the_result` |
| 시간/토큰 인자·호출 횟수 | `test_the_budget_arguments_reach_the_gateway` · `test_one_page_is_exactly_one_call` |
| HWP 좌표 바인딩 | `test_a_rendered_page_keeps_both_hashes` · `test_a_rendered_page_without_its_hash_is_refused` |
| HWP 바이너리 미전송 | `test_a_hwp_binary_is_not_a_page_image` |
| 입력 불량 → 외부 호출 0회 | `test_bad_input_never_reaches_the_model` (10 케이스) |
| 승인 밖 모델 | `test_a_model_we_did_not_ask_for_is_refused` |

### 시험이 실제로 무는지 확인했습니다

통과하는 시험은 아무것도 지키지 않습니다. 핵심 판정 넷을 코드에서 들어내고
돌렸습니다 → **4건 red** (좌표 불일치 · `max_tokens` 잘림 · 빈 전사 · 승인 밖 모델).
복구 → 44건 green.

## 6. 미검증 사항

1. **실제 모델을 한 번도 부르지 않았습니다.** 전부 fake router 입니다. 실제
   Vision 응답이 이 JSON 스키마를 지키는지, 표가 Markdown 으로 제대로 오는지는
   **대표 PDF 한 장으로 사람이 확인해야** 합니다(분담 §5-4)
2. **프롬프트의 전사 품질을 측정하지 않았습니다.** 숫자·단위·표 정렬이 맞는지는
   합성 시험으로 알 수 없습니다. 「계산하지 말라」 가 실제로 먹히는지도 실측 전입니다
3. **이미지 블록 모양을 실제 provider 로 검증하지 않았습니다**(§4-B).
   Anthropic 어댑터가 `content` 를 통과시키는 것은 코드로 확인했습니다
4. **폴백 차단은 사후 방어뿐입니다**(§4-A). 승인 밖 provider 로 **나가는 것 자체**는
   지금 막지 못합니다
5. **HWP 두 건은 건드리지 않았습니다.** `F0C9LHFTHLM` · `F0C9VH39NTW`
   (`converter_crashed`)의 원인 진단과 로컬 추출·렌더링은 Codex 몫입니다.
   코어는 **렌더링이 성공한 뒤의 페이지 이미지**만 받습니다
6. **`_unclosed()` 가 `intent._is_incomplete_json()` 과 같은 일을 합니다.**
   공용으로 빼지 않은 이유는 `intent.py` 가 이 작업의 소유 범위 밖이기 때문입니다.
   합치는 것은 통합 때 한 번에 하는 편이 낫다고 봅니다
7. 비용·처리 시간 실측 전입니다. 페이지당 입력 토큰이 이미지 해상도에 비례하므로,
   §4-C 의 해상도 상한이 사실상 비용 상한입니다

## 7. 회신이 필요한 것

- **§4-A 를 어느 쪽으로 가는지**: gateway 에 `allow_fallback` 을 더할지, 이 용도
  전용 `Router`(`fallback_models=()`)를 따로 둘지. 후자면 제 코어는 그대로입니다
- **추가 실패 코드 둘**(`coordinate_mismatch` · `model_not_approved`)을 원장
  스키마와 WC-21 §4 목록에 반영해 주십시오
- `VisionInputError` 를 원장에 **「호출 전 거절」** 로 적는 상태값이 필요합니다.
  WC-21 §4 의 상태 목록(`차단`?)에 들어갈지 판단 부탁드립니다

## 8. Codex 통합 진행 상황

2026-10-19 회신. 구현 증분은 아래 보안 경계까지다. DB 정책·승인/비용 원장·
queue/worker·렌더링·발행·색인은 아직 연결하지 않았고, 실제 모델을 호출하지 않았다.

- §4-A: `Router.restricted_to(model, sensitivity=...)`를 추가했다. 승인 모델의
  canonical ID 하나와 해당 provider만 가진 새 Router이며 폴백은 비어 있다.
  부모의 CostGuard는 공유해 지출을 분리 카운터로 초기화하지 않는다. 기존 Q&A의
  폴백 목록은 유지한다. 향후 worker는 정책/승인 대조 후 이 Router만 코어에 전달한다.
- §4-B: 현재 `openai_provider._content()`에는 이미 공통 base64 이미지 블록을
  `image_url`로 바꾸는 구현과 기존 gateway 시험이 있다. 새 SDK나 변환기를 중복
  추가하지 않았다. 실제 provider의 Vision 지원·정책 검증은 별개로 남아 있다.
- `archive/vision_policy.py`: 모델 호출 없는 fail-closed 사전 판정이다. 기본 OFF,
  모델/provider/credential/capability 확인, 원본/렌더링/ACL 확인, PII 차단 우선,
  페이지 이미지 해시와 승인 범위·만료/취소·정책 revision·예산 예약 범위를 대조한다.
  정책/승인/예약이 없으면 ready가 아니다. 이 순수 판정은 원자 예약이나 중복 전송
  방지를 구현한 것이 아니다. 새 worker는 신선한 DB 사실과 원자 claim을 별도로 가져야 한다.
- 사전 거절은 `preflight_rejected`, 정책 OFF는 `disabled`, 승인 없음은
  `approval_required`, 예약 없음/만료는 `budget_held`로 구분해 WC-21에 반영했다.
  모델 실패와 섞지 않는다. `coordinate_mismatch`/`model_not_approved`도 문서에 추가했다.
- `unreadable`의 120자 제한은 비식별화가 아니다. 본문이 들어갈 수 있으므로 원문과
  같은 권한으로 보관하고 운영 목록에는 건수만 노출한다. 문서와 코어 주석을 보완했다.
- 코어의 출력 검증을 보강했다. page_number/readable/markdown의 잘못된 타입과
  객체 밖 산문/배열을 거절하며, 미등록 품질 코드만 온 경우도 clean으로 올리지 않는다.
  음수/NaN/무한 비용은 unknown(None)으로 유지한다.
- 사진 설명 색인/원본 질의응답은 B-81로 분리해 대기 처리했다. 문서 전사나 이번
  승인 모델 관리가 사진 자동 전송을 활성화하지 않는다.

추가 시험 파일: `tests/test_vision_policy.py`, `tests/test_gateway_restricted.py`.
기존 `tests/test_vision_extract.py`에도 구조·품질·비용 경계 회귀를 추가했다.
Ruff(`src tests scripts`)와 diff 공백 검사는 통과했다. Python 시험은 Codex 실행 승인
서비스의 용량 오류로 두 번 모두 시작되지 않았다. **Claude의 앞선 44건 결과를
이 통합 변경의 통과 결과로 재사용하지 않는다.**

후속 확인: 오너가 위 코어/정책/gateway 집중 시험 138건 통과를 보고했다.
Codex도 실행 제한 해제 후 최초 원장 단위시험을 포함해 155건 통과를 확인했다.
DB 원장 구현 증분과 남은 검증은
[원장 진행 상황](2026-10-19-archiver-vision-ledger-status.md)에 기록한다.
HWP 두 원본의 종료 시그널/코드와 렌더링 가용성도
서버 확인 전이며, PDF/HWP 복구가 운영 완료됐다고 표시하지 않는다. 커밋하지 않았다.
