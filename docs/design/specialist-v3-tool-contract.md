# 전문 봇 호출 계약 v3 — 검색 도구를 주는 호출

> 작성: 2026-09-23
> 상태: **초안.** PF 개발자 2차 PR 의 선행조건이다. TY·PF 합의 후 고정한다.
> 근거: [Archiving Bot 분리와 Hermes 직접 호출](archiving-bot-separation-2026-09-23.md) §4.B,
> [PF Hermes 수정 요청](pf-hermes-archiver-integration-request.md) §3

## 1. v2 로는 왜 안 되나

v2 는 잘 돌아간다. 못 하는 것이 아니라 **다른 것을 하도록 설계됐다.**

```python
# specialist_wire.py
if not self.evidence:
    raise WireViolation("근거 없는 요청은 전문 봇에 보내지 않습니다.")
```

v2 의 전제는 「전문 봇은 스스로 자료를 가져오지 않는다」 다. 우리가 근거를 골라
보내고 전문 봇은 문장만 돌려준다. 그 전제가 인젝션으로 권한을 넘는 것을 막는다.

PF Hermes 의 가치는 **여러 번 찾아 읽는 것**이다. 한 번 검색해서 안 나오면 낱말을
바꾸고, 문서를 열어 보고, 옆 채널을 본다. v2 계약에 그대로 얹으면 그 능력이
사라진다 — 우리가 한 번 고른 근거만 받게 되므로, 남는 것은 문장 다듬기뿐이다.

그래서 v3 는 **찾는 일을 되돌려 주되, 찾는 주체는 그대로 둔다.**

## 2. 핵심 결정 — 새 broker 를 짓지 않는다

TYBot 에는 이미 `specialist_tools.ToolBox` 가 있다. 지금 내부 Hermes 가 쓰는 것이고,
필요한 도구가 전부 있다.

| 도구 | 하는 일 |
|---|---|
| `search` | 권한 안에서 원문 검색 |
| `read_channel` | 채널 원문 읽기 |
| `read_document` | 문서 본문 읽기 |
| `fetch_recent_slack` | 실시간 최근 대화 |

여기에 예산(`ToolBudget`), 거절 문자열, 「예외를 밖으로 내지 않는다」 가 이미 붙어
있다. **v3 는 이것을 프로세스 밖으로 여는 것이지 새로 짓는 것이 아니다.**

이유는 편의가 아니다. ACL 판정이 두 곳에 있으면 **둘이 갈라진다.** 한쪽만 고쳐지는
날이 오고, 그때 어느 쪽이 맞는지 판정할 근거가 없다. 내부 Hermes 와 외부 Hermes 가
같은 코드로 권한을 받아야 「내부에서는 안 보이는데 외부에서는 보인다」 가 안 생긴다.

```
       TYBot Master                         PF Hermes (TY DMZ, Node)
  ┌──────────────────────┐            ┌────────────────────────────┐
  │ ctx = RequestContext │            │                            │
  │ ToolBox(ctx, budget) │            │  질문을 받고                 │
  │                      │            │  검색 → 읽기 → 검색 → 답변    │
  │  ① /v3/answer  ──────┼───────────▶│                            │
  │                      │            │                            │
  │  ② /v3/tools/call ◀──┼────────────┤  도구 호출 (tool token)      │
  │     ToolBox.run()    │            │                            │
  │     → 본문 + ev ID ──┼───────────▶│                            │
  │                      │            │                            │
  │  ③ 답변 + ev ID ◀────┼────────────┤                            │
  │     ID·권한 재검증     │            │                            │
  └──────────────────────┘            └────────────────────────────┘
        두 방향 모두 같은 호스트의 Unix socket. 새 인바운드 포트 없음.
```

## 3. 도구 토큰 — 서 있는 자격증명을 주지 않는다

Hermes 가 우리를 되부르려면 자격이 필요하다. **상시 키를 주지 않는다.**

요청마다 **도구 토큰** 하나를 만들어 요청 본문에 넣는다.

| 성질 | 값 | 왜 |
|---|---|---|
| 유효 범위 | 그 `request_id` 하나 | 다른 요청의 근거를 못 가져온다 |
| 수명 | `deadline_ms` 와 같이 끝남 | 답이 끝났는데 계속 읽을 이유가 없다 |
| 권한 | 만들 때의 `ctx` 에 묶임 | 토큰이 권한을 정하지 않는다. 인자로 바꿀 수 없다 |
| 재사용 | 불가. 답변 반환 시 즉시 폐기 | 답을 보낸 뒤의 호출은 요청 밖이다 |

토큰은 `authorization_id` 를 **대신하지 않는다.** `authorization_id` 는 「누구 권한으로
판정했나」 의 이름이고, 토큰은 「이 요청이 지금 살아 있나」 의 열쇠다. 둘을 하나로
합치면 권한 판정 기록이 수명에 끌려다닌다.

### 권한은 매 호출 다시 본다
토큰이 가리키는 `ToolBox` 는 `ctx` 를 갇힌 채로 들고 있고, `store.visible_docs(ctx)` 를
**호출마다** 다시 계산한다. 그래서 답변 도중 채널에서 빠진 사람은 그 다음 도구
호출부터 못 읽는다. 스냅샷을 떠서 들고 있지 않는다.

## 4. 요청 (`POST /v3/answer`)

v2 요청에서 두 가지가 달라진다. `evidence` 가 **선택**이 되고, `tools` 가 생긴다.

```json
{
  "schema": "tybot.specialist.request.v3",
  "request_id": "8f1d…",
  "authorization_id": "auth-2026-09-23-…",
  "workspace": "tyit",
  "question": "지난주 정산 금액이 얼마였지",
  "evidence": [
    {"id": "e1", "text": "…"}
  ],
  "follow_up": [
    {"id": "e0", "locator": "opaque-…"}
  ],
  "tools": {
    "token": "…",
    "endpoint": "/v3/tools/call",
    "allow": ["search", "read_channel", "read_document", "fetch_recent_slack"],
    "budget": {
      "max_calls": 12,
      "max_per_tool": 4,
      "max_chars": 60000,
      "max_seconds": 45
    }
  },
  "limits": {
    "deadline_ms": 90000,
    "max_output_chars": 3000
  }
}
```

### evidence 를 선택으로 바꾸는 이유
v2 는 빈 근거를 거부했다. 그 규칙이 막던 것은 「전문 봇이 자기 색인이나 기억으로
답하는 것」 이다. v3 에서는 그 위험이 **도구 쪽으로 옮겨 갔다** — 도구를 통해서만
자료를 얻고, 도구는 우리가 권한을 확인한 것만 준다. 그래서 씨앗 근거가 없어도 된다.

대신 **답변에 근거 ID 가 하나도 없으면 그 답은 버린다**(§6). 규칙이 사라진 것이
아니라 판정 시점이 요청에서 응답으로 옮겼다.

### follow-up 은 본문이 아니라 좌표다
후속 질문("방금 그 문서 다시")에서 이어 가는 것은 이전 **답변 문장**이 아니라 그
답변이 읽은 **원문 좌표**다(절대 원칙 1). 좌표는 opaque 하고, 열 때 현재 권한으로
다시 확인한다. 권한이 바뀌었으면 안 열린다.

### deadline 이 v2 보다 긴 이유
v2 는 15초다. 근거를 다 받아 문장만 만들기 때문이다. v3 는 검색·읽기를 여러 번
하므로 그 시간으로는 첫 검색도 못 끝낸다. 90초를 기본으로 두되 **예산이 먼저
끝나게** 설계한다 — 시간으로만 끊으면 절반 읽은 상태에서 잘린다.

## 5. 도구 호출 (`POST /v3/tools/call`)

Hermes → TYBot 방향. 같은 Unix socket, 같은 HMAC 서명 규칙.

```json
{
  "schema": "tybot.specialist.tool.v3",
  "request_id": "8f1d…",
  "token": "…",
  "call_id": "t1",
  "tool": "search",
  "args": {"query": "정산", "where": "#팀-자금_abb540-주간보고"}
}
```

응답:

```json
{
  "schema": "tybot.specialist.tool_result.v3",
  "call_id": "t1",
  "ok": true,
  "text": "…권한 안에서 찾은 것…",
  "evidence": [
    {"id": "e7", "kind": "channel_line"},
    {"id": "e8", "kind": "document"}
  ],
  "budget": {"calls_left": 11, "chars_left": 52000, "seconds_left": 41}
}
```

### 실패도 `ok: true` 로 온다
도구가 터지거나 예산이 끝나도 **HTTP 오류로 답하지 않는다.** `text` 에 사람 말로
적어 보낸다 — `ToolBox.run()` 이 이미 그렇게 한다.

```
(검색 예산을 다 썼습니다. 더 찾지 말고 지금까지 읽은 것으로 답하세요.
 자료가 없다고 단정하지 말고, 어디까지 찾아봤는지 한 줄 적으세요.)
```

이유: 모델이 도구 실패를 **모른 채** 답을 만드는 것이 가장 나쁘다. 오류로 끊으면
Hermes 쪽 예외 처리에 맡기게 되고, 그쪽이 조용히 삼키면 우리는 알 방법이 없다.

`ok: false` 는 **계약 위반**에만 쓴다 — 토큰 만료·모르는 도구·요청 불일치. 그때는
Hermes 가 재시도하면 안 되고 지금까지 읽은 것으로 답해야 한다.

### 돌려주지 않는 것
채널 ID, 파일 경로, 문서 경로, `message_ts`, Slack 토큰, DB 자격. Hermes 가 받는
식별자는 **이 요청 안에서만 유효한 opaque ID** 다. 모아도 우리 구조가 재구성되지
않는다(v2 `Evidence` 의 이유와 같다).

## 6. 응답 (`POST /v3/answer` 의 반환)

```json
{
  "schema": "tybot.specialist.response.v3",
  "request_id": "8f1d…",
  "status": "answered",
  "answer": "…",
  "used_evidence": ["e7", "e8"],
  "uncertain": [],
  "usage": {
    "tool_calls": 5,
    "input_tokens": 18000,
    "output_tokens": 600,
    "cost_usd": 0.041
  }
}
```

`status` 는 넷이다. **「모른다」 와 「못 찾았다」 와 「고장났다」 를 가른다** — 셋을
하나로 묶으면 화면에서 원인을 못 가린다.

| status | 뜻 | Master 가 하는 일 |
|---|---|---|
| `answered` | 근거로 답했다 | 출처를 붙여 전송 |
| `no_evidence` | 찾았지만 근거가 없다 | 「없다」 로 전하되 어디까지 찾았는지 함께 |
| `refused` | 권한·계약 때문에 답하지 않았다 | 사유를 전한다. 마스터 폴백 없음 |
| `failed` | 도중에 끊겼다 | 마스터 폴백 |

### Master 가 다시 보는 것
Hermes 가 `used_evidence` 로 준 ID 를 **우리가 발급한 목록과 대조**한다. 없는 ID 가
섞여 있으면 그 답은 버린다 — 지어낸 근거이거나 다른 요청의 것이다.

그리고 **ID 를 실제 좌표로 되돌릴 때 권한을 다시 본다.** 답변 도중 권한이 바뀌었을
수 있고, 출처 링크는 사람이 눌러서 가는 곳이다.

`used_evidence` 가 비어 있으면 근거 없는 답이다(§4). 버린다.

## 7. 서명·재생 방지

v2 와 **같은 규칙**을 쓴다(`specialist_wire.canonical`). 두 벌로 두면 한쪽만 고쳐진다.

- `canonical(method, path, timestamp, nonce, body)` 에 HMAC-SHA256
- timestamp 허용 오차 밖이면 거절, nonce 재사용 거절
- Unix socket 권한이 1차, HMAC 이 2차. 소켓 권한만 믿으면 그 사용자로 도는 아무
  프로세스나 호출할 수 있다

도구 호출 방향도 같다. **다만 키가 다르다** — Hermes → TYBot 방향은 도구 토큰이
인증이고, HMAC 키는 양방향 공유 비밀이다. 토큰만으로 충분해 보이지만, 토큰은
요청 본문에 있으므로 로그에 남을 수 있다. 둘을 같이 쓴다.

## 8. 취소 전파

Master 가 deadline 에 닿거나 사용자가 스레드를 떠나면 요청을 취소한다. 그때:

1. 도구 토큰을 즉시 폐기한다 → 다음 도구 호출이 `ok: false` 로 막힌다
2. `POST /v3/cancel` 로 알린다 (best effort)
3. 응답이 와도 버린다

**1번이 본체다.** 취소 통지가 안 닿아도 도구가 막히므로 Hermes 는 더 읽지 못하고,
읽지 못하면 새 근거로 답할 수 없다. 통지에만 기대면 통지를 놓친 순간 무제한이 된다.

## 9. 우리가 만들 것 / PF 가 만들 것

| 쪽 | 만들 것 |
|---|---|
| TY | `/v3/answer` 클라이언트, 도구 토큰 발급·폐기, `/v3/tools/call` 서버(ToolBox 를 감싼다), 응답 ID 재검증, fixture |
| PF | `specialist` 모드, `/v3/answer` 서버, 도구 클라이언트, 예산 준수, status 네 가지 구분 |

**계약 fixture 를 우리가 먼저 낸다.** PF 가 맞출 대상이 우리 구현이면, 우리 코드를
고칠 때마다 그쪽이 깨진다. 고정된 JSON 이 기준이어야 양쪽이 각자 시험한다.

## 10. 합의가 필요한 것 (열린 질문)

1. **예산 기본값** — `max_calls` 12, `max_seconds` 45 는 내부 Hermes 관찰값에서
   가져온 추정이다. PF 의 실제 검색 횟수를 보고 정한다
2. **`fetch_recent_slack` 을 줄 것인가** — 실시간 대화는 아카이브에 없는 것이다.
   주면 Hermes 가 최신을 볼 수 있고, 안 주면 「방금 올린 건 왜 모르나」 가 남는다
3. **부분 답변** — deadline 에 걸렸을 때 지금까지로 답할지 `failed` 로 끝낼지
4. **비용 귀속** — Hermes 가 쓰는 모델 비용은 프금팀 계정이다(오너 계획 §7.1).
   `usage.cost_usd` 는 보고용이고 우리가 상한을 걸지 않는다

## 11. 하지 않는 것

- **archive mount 를 주지 않는다.** 경로를 주면 ACL 이 파일 권한으로 내려가고,
  그때 「누가 무엇을 봤나」 를 우리가 못 말한다
- **Slack 토큰·DB 자격을 주지 않는다.** Hermes 가 직접 Slack 을 읽으면 수집 주인이
  둘이 된다(분리 결정의 반대)
- **새 인바운드 포트를 열지 않는다.** 같은 호스트 Unix socket 만(§5)
- **PF 소스를 Python 으로 부분 복사하지 않는다.** fork 를 계속 고치면 두 벌이 되고,
  그게 이 계약을 만드는 이유 자체를 없앤다
