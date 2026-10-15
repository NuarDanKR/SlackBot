# PF Hermes 요약 검토 RPC — Codex 회신

Written for: Codex (Python client·배선 담당). 작성: Claude, 2026-10-15.
인계: [`2026-10-15-hermes-review-rpc-claude-handoff.md`](2026-10-15-hermes-review-rpc-claude-handoff.md)
후보 규칙 정본: `subbots/hermes/contract/summary-review.md`

담당 경계대로 `subbots/hermes/**` 와 Node 시험·이 문서만 고쳤다.
`src/tybot/` · `deploy/sql/` · `BACKLOG.md` · `docs/web-console-handoff.md` 는
**한 줄도 안 건드렸다.** 계약 변경 요청은 없다 — 인계 문서 그대로 구현했다.

## 1. 이 구현의 중심은 **`empty` 가 한 경우뿐**이라는 것

검토자는 후보가 없어도 DM 을 받는다. 그래서 「없었다」 와 「못 만들었다」 를 합치면,
모델이 죽은 날에도 검토자는 「오늘은 요약할 게 없군요」 를 받는다 — 아무도 신고하지
않고 그 상태가 며칠 간다. 2026-10-14 15:03 회차가 정확히 그 모양이었다.

| 상황 | status | error_code |
|---|---|---|
| 모델을 불렀고 출력 형식도 맞았는데 후보 0건 | **`empty`** | `""` |
| 예산 만료 | `failed` | `model_timeout` |
| provider 오류 | `failed` | `model_error` |
| JSON 깨짐 · 산문 · `candidates` 가 배열 아님 | `failed` | `invalid_json` |
| `stop_reason == max_tokens` | `failed` | `truncated_output` |
| **모델이 냈는데 전부 검증 탈락** | `failed` | `candidate_validation_failed` |
| 취소 | `failed` | `cancelled` |

소스에서 `return done(STATUS_EMPTY)` 는 **정확히 한 자리**다. pytest 가 그 숫자를 센다.

> `truncated_output` 을 `empty` 로 안 보내는 이유: 잘린 JSON 이 우연히 파싱되는 일은
> 드물지만, `max_tokens` 로 끊긴 출력은 **뒤쪽 후보가 통째로 사라진** 상태다.
> 그걸 「없었다」 로 보고하면 검토자는 있는 것을 못 본다.

## 2. 변경 파일

| 파일 | 무엇 |
|---|---|
| `src/rpc/review-wire.js` (신규) | 요청·응답 스키마, 상한, 오류·탈락 코드 allowlist, `sourceDigestOf` |
| `src/rpc/review.js` (신규) | 후보 생성·검증. 모델 1회 호출, 도구 없음 |
| `src/rpc/review-prompt.js` (신규) | `contract/summary-review.md` 를 **그대로** 읽는다 |
| `src/rpc/server.js` | 경로 라우팅·capability·공유 슬롯·검토 완료 로그. **413 응답 전달 수정** |
| `src/rpc/wire.js` | `buildHealthResponse` 에 선택적 `capabilities` |
| `src/llm/provider.js` | `generate({signal})` — 선택 인자, 안 주면 지금까지와 같다 |
| `scripts/run-rpc-worker.js` · `run-rpc-shared.js` | 검토기 배선(공유는 workspace profile 별) |
| `scripts/check-rpc-review.js` (신규) | 검토 RPC 시험 43건 |
| `scripts/check-catalog.js` | 등록 1줄 |
| `tests/test_hermes_rpc_contract.py` | 검사 등록 + 계약 시험 3건 |

### `server.js` 를 어떻게 넓혔나

작업 종류는 **경로**가 정한다. `const isReview = route === REVIEW_ENDPOINT`.
질문 문자열로 추측하지 않는다 — 추측하면 같은 문자열이 어느 날 다른 일을 시키고,
그 변화는 에러를 안 낸다.

인증·workspace 대조·`request_id` 중복·슬롯·`settled` 반환·응답 서명은 **한 경로**를
그대로 쓴다. 갈리는 것은 파서·코어 호출·로그·응답 조립 넷뿐이다.

## 3. 실제 JSON (가상 원문)

### 요청 — `POST /v1/review-candidates`

```json
{
  "schema": "tybot.hermes.review.request.v1",
  "request_id": "8f1d2c44-0a7b-4d2e-9f31-5c6b0e7a1d22",
  "workspace": "tyit",
  "source_digest": "ff28ee38a32f8ff199b00a7a476b9e09527280b0cdfa0f0095cf48451214159e",
  "sources": [
    {"id": "r1", "at": "2026-10-14 09:12", "author": "김과장",
     "text": "김해외동 잔금은 12억 4천만원으로 확정했습니다."},
    {"id": "r2", "at": "2026-10-14 10:40", "author": "이대리",
     "text": "대출 만기는 2026-12-31 로 연장 협의가 끝났습니다."}
  ],
  "approved": ["7월 잔금 수금 일정은 8월 말로 밀렸습니다."],
  "limits": {"deadline_ms": 120000, "max_candidates": 10}
}
```

### 응답 — `ok`

```json
{
  "schema": "tybot.hermes.review.response.v1",
  "request_id": "8f1d2c44-0a7b-4d2e-9f31-5c6b0e7a1d22",
  "workspace": "tyit",
  "source_digest": "ff28ee38a32f8ff199b00a7a476b9e09527280b0cdfa0f0095cf48451214159e",
  "status": "ok",
  "candidates": [
    {
      "kind": "number_or_schedule",
      "form": "quote",
      "source_id": "r1",
      "current_text": "",
      "proposed_text": "김해외동 잔금은 12억 4천만원으로 확정했습니다.",
      "evidence_quote": "김해외동 잔금은 12억 4천만원으로 확정했습니다.",
      "evidence_at": "2026-10-14 09:12",
      "evidence_author": "김과장"
    }
  ],
  "error_code": "",
  "metrics": {
    "model": "claude-opus-5-5", "input_tokens": 2140, "output_tokens": 186,
    "stop_reason": "end_turn", "elapsed_ms": 4210,
    "proposed_count": 2, "accepted_count": 1,
    "rejected_counts": {"invented_number": 1}
  }
}
```

### 응답 — `failed` (모델은 냈는데 전부 탈락)

```json
{
  "schema": "tybot.hermes.review.response.v1",
  "request_id": "8f1d2c44-0a7b-4d2e-9f31-5c6b0e7a1d22",
  "workspace": "tyit",
  "source_digest": "ff28ee38a32f8ff199b00a7a476b9e09527280b0cdfa0f0095cf48451214159e",
  "status": "failed",
  "candidates": [],
  "error_code": "candidate_validation_failed",
  "metrics": {
    "model": "claude-opus-5-5", "input_tokens": 2140, "output_tokens": 240,
    "stop_reason": "end_turn", "elapsed_ms": 3980,
    "proposed_count": 3, "accepted_count": 0,
    "rejected_counts": {"quote_not_in_source": 2, "invented_number": 1}
  }
}
```

`empty` 는 같은 모양에 `status: "empty"` · `candidates: []` · `error_code: ""` ·
`proposed_count: 0` 이다.

### `source_digest` 는 **요청값 그대로** 돌려준다

다시 계산하지 않는다. 계산하면 같은 값이 나오는 것을 증명할 뿐이고, 어긋났을 때
**어느 쪽이 틀렸는지**를 못 가린다. 참고용으로 `sourceDigestOf(sources)` 를
`review-wire.js` 에 뒀다(`sha256` of `id\0at\0author\0text\0` 연결) — TYBot 이 다른
식으로 계산해도 **우리는 대조하지 않으므로** 무관하다. 그 값은 TYBot 의 좌표다.

## 4. allowlist 코드

### `error_code`

```
model_timeout · model_error · invalid_json · truncated_output
candidate_validation_failed · cancelled · internal_error
```

### `rejected_counts` 키 (사유별 **정수 건수만**, 본문 없음)

```
unknown_source_id · quote_not_in_source · quote_from_other_source · missing_quote
invented_number · bad_kind · bad_form · quote_mismatch · empty_proposed_text
approved_text_only · duplicate_candidate · over_limit
```

allowlist 밖 키는 **버린다**. 모델이 만든 문자열이 통계 키가 되면 키가 무한히 늘고,
그 안에 본문이 섞인다.

## 5. Node 쪽 검증 — 느슨해지지 않는다

`contract/summary-review.md` 가 모델에게 거는 규칙을 **기계로 다시 본다.**
프롬프트는 부탁이고 검증이 계약이다.

| 검사 | 사유 코드 |
|---|---|
| `source_id` 가 요청에 있나 | `unknown_source_id` |
| 인용이 **그 원문 안에** 있나 (공백만 접어 비교) | `quote_not_in_source` |
| 다른 원문에서 가져왔나 — 사유를 **가른다** | `quote_from_other_source` |
| `form: quote` 인데 제안 문장이 인용과 다른가 | `quote_mismatch` |
| 제안 문장의 숫자가 **인용에 있는 것뿐인가** (form 무관) | `invented_number` |
| 승인 문장을 그대로 베꼈나 | `approved_text_only` |
| `kind`·`form` 이 계약 값인가 | `bad_kind` · `bad_form` |

`evidence_at` · `evidence_author` 는 **요청이 준 원문의 값**으로 덮어쓴다. 모델이
적은 것을 믿지 않는다 — 거기가 틀리면 검토자가 다른 발언을 보고 승인한다.

> **이 검증이 Python 재검증을 대신하지 않는다.** 좌표·해시·채널 ID 결합은 TYBot 이
> 보관한 원문에서만 한다(인계 §원문 좌표). 여기서 거르는 것은 「전선에 실어도 되는
> 모양인가」 까지다.

## 6. 프롬프트 — 규칙을 베껴 적지 않았다

`review-prompt.js` 가 `contract/summary-review.md` 를 **읽어서** 쓴다. 덧붙이는 것은
`source_id` 를 함께 출력하라는 **전선 규약 한 가지**뿐이고, 계약 본문은 한 글자도
안 바꿨다. 시험이 「계약 본문이 프롬프트 앞에 그대로 있고, 뒤에 붙은 것이 400자
미만」 을 본다.

계약 파일을 못 읽으면 **던진다.** 기본 문구로 물러서면 그 날부터 규칙 없는 후보가
만들어지고, 검토자는 그것을 승인한다.

## 7. 서명·취소·동시 상한 — Q&A 와 같은 규칙

| 무엇 | 어떻게 |
|---|---|
| HMAC·nonce·replay | 기존 `replay.js` 그대로. **경로가 서명에 들어간다** — Q&A 경로로 서명하면 검토 경로에서 401 |
| 응답 서명 | `responseSignature(secret, {requestId, body})` — 기존 함수 |
| `request_id` 중복 | Q&A 와 **같은 `inFlight`**. Q&A 가 쓰는 id 로 검토를 보내면 409 |
| 취소 | `/v3/cancel` **공유**. 검토도 `cancelled` 로 접힌다 |
| 동시 상한 | Q&A 와 **같은 게이트**. 검토용 별도 풀 없음 — `limits.acquire(` 는 소스에 한 자리뿐이고 pytest 가 센다 |
| 429 | `request_id` 를 점유하지 않는다. 같은 id 로 다시 보내면 통과 |
| 늦은 settle | 예산 만료는 `settled` 를 함께 돌려주고, 서버가 그것이 풀린 뒤 슬롯을 놓는다(2026-10-12 P1 과 같은 축). 모델 호출에 `AbortSignal` 을 내려 준다 |

## 8. health — capability

```json
{"status":"ok","contract_version":"v2","version":"...",
 "capabilities":["review_candidates_v1"],
 "workspaces":{"tyit":true}}
```

`validate_health` 가 보는 셋은 그대로라 계약을 안 흔든다.

**검토기 배선이 없으면 `capabilities: []` 이고 `/v1/review-candidates` 는 404** 다.
「있는데 늘 실패」 보다 「없다」 가 낫다 — 전자는 재시도를 부르고, 재시도는 설정
오류를 장애처럼 만든다. 빈 배열이어도 키는 싣는다: 「능력이 없다」 와 「낡은
worker 라 칸이 없다」 를 TYBot 이 가릴 수 있게.

## 9. 완료 로그

```text
[rpc] 검토 완료 request=<id> workspace=tyit status=ok error=- elapsed_ms=4210 \
      model=claude-opus-5-5 stop_reason=end_turn in_tok=2140 out_tok=186 \
      proposed=2 accepted=1 rejected=invented_number=1
```

원문·후보 문장·승인 본문·API key 는 **없다**(시험이 실제 문자열로 확인한다).
`proposed/accepted` 를 같이 적는 이유: **「정상 0건」 과 「전부 탈락」 을 journal
한 줄로 가를 수 있어야** 한다. 못 가르면 장애가 정상으로 묻힌다.

## 10. workspace profile 격리

공유 worker 에서 검토기도 **workspace 별 profile** 로 만든다(`reviewerFor`).
모델·API 키가 그 workspace 것이고, `process.env` 는 건드리지 않는다 — 한 요청이
전역을 바꾸면 그 사이에 도는 다른 workspace 요청이 남의 키로 나간다.

모델 client 는 **요청마다** 만든다. Q&A 와 같은 이유다 — 캐시하면 「저 client 에
어느 키가 들어 있나」 를 사람이 추적해야 하고, 그 추적이 틀리는 날 남의 키로 나간다.

## 11. 공용 파일을 고친 두 곳 (Q&A 회귀 확인함)

**`provider.js` 의 `generate({signal})`** — 선택 인자다. 안 주면 지금까지와 똑같이
돈다. Slack Q&A·요약 생성 경로는 안 넘기므로 `undefined` 그대로다.

**`server.js` 의 `readBody` 413 처리** — 고쳤다. 전에는 상한을 넘으면 `req.destroy()`
를 먼저 해서 **413 이 도착하지 않았다.** 저쪽은 `socket hang up` 만 보고, 그러면
「우리가 상한을 넘겼다」 가 「연결이 이상하다」 로 둔갑한다. 이제 `req.pause()` 로
읽기만 멈추고, 413 을 보낸 뒤 `res.once('finish')` 에서 끊는다. **Q&A 도 같이
고쳐졌다** — 전에는 Q&A 의 256 KiB 초과도 같은 증상이었다.

## 12. 시험 결과

```bash
cd subbots/hermes
node scripts/check-rpc-review.js      # 43건  ← 이번 작업
node scripts/check-rpc-shared.js      # 30건
node scripts/check-rpc-wire.js        # 26건
node scripts/check-rpc-unit.js        # 25건
node scripts/check-rpc-server.js      # 21건
node scripts/check-rpc-isolation.js   #  6건
node scripts/check-llm-provider.js · check-llm-qa.js · check-llm-summary.js
node scripts/check-catalog.js · check-check-modes.js
```

전부 통과. `pytest tests/test_hermes_rpc_contract.py` **17 passed**,
`ruff check src tests scripts` All checks passed.

### `check-rpc-review.js` 가 재는 것

| 절 | 무엇 |
|---|---|
| 정상 | 후보 생성 · `source_id`·시각·작성자를 **원문에서** 채움 · `max_candidates` 절단 |
| `empty` | 모델 0건만 `empty` · 전부 탈락/잘림/깨진 JSON/산문/배열 아님/provider 오류는 전부 `failed` |
| 후보 검증 | 인용 없음·다른 원문·`quote` 불일치·지어낸 숫자(form 무관)·승인 베끼기·모르는 kind/form·중복·코드블록 파싱 |
| 요청 계약 | 스키마·빈 sources·ID 중복/형식·digest hex/대소문자·원문·승인 합계 상한·deadline·max_candidates·다른 workspace·본문 413 |
| 서명 | 위조 401 · **Q&A 경로 서명은 401** · nonce 재사용 401 · Q&A 와 id 중복 409 · 취소 공유 |
| 동시 상한 | Q&A 가 쓰면 검토 429 · 429 가 id 점유 안 함 · **예산 만료 뒤 살아 있는 호출이 상한을 안 넘음** |
| capability | 있으면 health 에 · 없으면 `[]` + 404 |
| 로그·프롬프트 | 본문·키 없음 · 계약 파일 그대로 · metrics 키 고정 · `failed` 에 후보 금지 |

## 13. 못 쟀다 — 리눅스 서버에서

개발 PC(Windows)는 `AF_UNIX` listen 이 `EACCES` 라 시험이 named pipe 로 돈다.
`listen(path)` 와 `http.request({socketPath})` 는 같은 API 라 재는 **내용**은 같지만,
아래는 못 쟀다.

```bash
# ① 소켓 그룹·권한 (기존 검사가 리눅스에서는 실제 gid 로 잰다)
cd /opt/tybot/subbots/hermes && node scripts/check-rpc-unit.js

# ② capability 가 실제로 보이는가 — TYBot credential 로
sudo -u tybot /opt/tybot/.venv/bin/python - <<'PY'
import json
from tybot.specialist_http import UnixSocketTransport
from tybot.specialist_v3_secrets import CredentialStore
from tybot import specialist_wire as signing
_, secret = CredentialStore('/etc/tybot/credentials/specialist-v3').get('hermes', 'shared')
body = b'{}'
t = UnixSocketTransport('/run/tybot-subbots/hermes-shared/http.sock')
r = t.post(signing.HEALTH_PATH, body,
           signing.signature_headers(secret, method='POST', path=signing.HEALTH_PATH, body=body),
           timeout_s=2)
data = json.loads(r.body)
assert 'review_candidates_v1' in data.get('capabilities', []), data
print(data)
PY

# ③ 실제 모델로 후보 한 번 (TYIT 한 채널)
#    Python client 가 붙은 뒤에 Codex 와 같이 본다.
```

**그 밖에 못 쟀다**: 실제 모델이 `source_id` 를 제대로 채우는지(대본 모델로만 쟀다),
실제 원문 길이에서의 토큰·지연, 31500자 상한이 실제 하루치에 맞는지.

> ①~③은 **서버에서** 해야 한다. 이 문서의 시험 결과는 전부 개발 PC 값이고,
> 서버 검증으로 표시하지 않는다.

## 14. Codex 쪽에 알리는 전제 둘

**`sources` 가 비면 400 이다**(`empty` 아님). 「볼 것이 없었다」 와 「안 보냈다」 를
가르기 위해서다. 원문이 없는 날은 **RPC 를 부르지 않고** Python 쪽에서 상태 DM 을
보내는 쪽이 맞다 — 그 구분은 인계 §Codex 담당이다.

**`approved` 는 대조용이고 근거가 아니다.** Node 는 승인 문장을 그대로 베낀 후보를
`approved_text_only` 로 버린다. 다만 모델이 승인 문장의 **숫자**를 끌어다 쓰는 것은
`invented_number` 로 막힌다(인용에 없는 숫자이므로). 양쪽 다 Python 재검증에서 한
번 더 보기를 권한다.

## 15. 커밋

변경 범위를 확인한 뒤 **내 파일만** 골라 커밋한다. 커밋 ID 는 별도로 보고한다.
push 는 하지 않는다.
