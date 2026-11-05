# B-12 임베딩 프로바이더 스모크 — 실제 API 계약 확인

작성 2026-11-05 · 신규 2 + 이 문서. **기존 파일 수정 0.** 커밋하지 않았다.
**실제 네트워크 호출은 하지 않았다** — 아래 명령은 서버에서 사람이 친다.

| | |
|---|---|
| `scripts/check_embedding_provider.py` | 도구 |
| `tests/test_check_embedding_provider.py` | 36건, 가짜 SDK |

## 왜 필요한가

게이트웨이 경계는 **가짜 SDK 로만** 검증돼 있다. 진짜 API 가 같은 모양으로
답하는지는 아무도 안 봤다 — 응답 필드 이름 하나가 달라도 **첫 색인 실행에서야**
드러나고, 그때는 이미 돈이 나간 뒤다. 그 한 번을 미리 친다.

진단 스크립트이므로 콘솔 규칙의 예외다(CLAUDE.md 「운영 기능은 콘솔에 둔다」 —
「진단 스크립트는 예외다」).

## 돌리는 법

### 1. 계획만 — 아무것도 안 보낸다 (기본)

```bash
cd /opt/tybot && sudo -u tybot /opt/tybot/.venv/bin/python \
  scripts/check_embedding_provider.py \
  --provider openai --model <레지스트리에 적은 모델> --dimensions <차원>
```

네트워크 0회, SDK import 0회, 키 조회 0회. **무엇을 보낼지만** 적는다.

```
mode                   plan
provider_requested     openai
model_requested        …
dimensions_requested   1536
timeout_seconds        60.0
inputs                 3
key_checked            False
calls_made             0
result                 planned
```

### 2. 실제 호출 — `--execute` 가 있어야만

```bash
cd /opt/tybot && sudo -u tybot env \
  TYBOT_ENV_FILE=/etc/tybot/tybot.env \
  OPENAI_API_KEY="$(cat /경로/키파일)" \
  /opt/tybot/.venv/bin/python \
  scripts/check_embedding_provider.py \
  --provider openai --model <모델> --dimensions <차원> \
  --timeout 30 --execute
```

- 키를 **명령줄 인자로 적지 않는다.** `ps` 에 보인다. 환경변수나
  `TYBOT_ENV_FILE` 로 넘긴다
- 운영 비밀 저장소(DB + 암호화 키)로 보려면 `--key-from secret-store`
- 비용까지 보려면 `--price-per-mtok 0.02` 처럼 단가를 준다. **안 주면 비용은
  `cost_confirmed=false` 로 남는다. 0 이 아니다**
- 콘솔·자동화에 물리려면 `--json`

성공하면 이렇게 나온다.

```
mode                   execute
calls_made             1
identity_matched       True
vectors_returned       3
vector_widths          [1536]
cost_confirmed         False
cost_usd               None
result                 ok
```

종료 코드: 계획·성공 `0`, 거절 `1`.

## 안 하는 것

| | |
|---|---|
| **아카이브·QA 로그·DB 원문** | 하나도 안 읽는다. 모델 레지스트리도 안 본다 |
| **모델 선택** | provider·model·dimensions 를 **사람이 명시**해야 한다. 기본값 없다 — 무엇을 어디로 보내는지가 명령줄에 그대로 남는다 |
| **재시도·폴백** | 정확히 1회. 재시도하는 진단은 **찾으려던 간헐 고장을 가리고** 청구만 두 배로 한다 |
| **completion 우회** | `SMOKE_PROVIDERS` 에 `openai` 뿐. anthropic 은 완성(completion)을 파는 곳이라 없다 |

## 보내는 것

코드에 박아 둔 합성 문장 **셋**. 파일이나 DB 에서 읽어 오는 순간 이 도구는
「아무 글이나 외부로 보내는 길」 이 되고, 그 길은 한 번 열리면 닫기 어렵다.
회사 정보·사람 이름·숫자가 없는 문장만 쓴다.

```
This sentence exists only to measure one embedding call.
Rain is expected on the north side of the valley tomorrow.
이 문장은 임베딩 호출의 모양을 재어 보기 위한 것이다.
```

**출력에는 안 나온다.** 개수만 나온다.

## 출력에 없는 것

붙여 넣어도 안전해야 한다 — 이 도구의 결과는 티켓과 채팅으로 돌아다닌다.

- 보낸 문장
- 벡터 값(폭만 나온다 — 벡터는 본문에서 나온 값이다)
- API 키
- **프로바이더 오류 본문.** 임베딩 엔드포인트의 4xx 는 거절한 입력을
  되돌려 주는 일이 있다. 나가는 것은 고정 낱말 하나뿐이다

## 실패 코드

두 집합이 **안 겹친다.** 조치가 다르기 때문이다 — `smoke_*` 는 **나가지도
못했다**, 나머지는 **프로바이더가 답했는데 모양이 틀렸다**.

| 코드 | 뜻 |
|---|---|
| `smoke_arguments_invalid` | 차원·timeout·단가가 범위 밖 |
| `smoke_provider_unsupported` | 이 도구가 띄울 수 있는 어댑터가 아님 |
| `smoke_api_key_missing` | 키가 없거나 공백 — **호출 전에** 멈춘다 |
| `embedding_identity_mismatch` | 돌려준 provider·model 이 요청과 다름 |
| `embedding_result_incomplete` | 벡터 개수가 안 맞음(잘림·중복 index) |
| `embedding_dimension_mismatch` | 차원이 레지스트리 값과 다름 |
| `embedding_vector_invalid` | 유한한 수가 아닌 값 |
| `embedding_call_timeout` | 시간 안에 안 끝남 |
| `embedding_call_failed` | 그 밖의 프로바이더 오류(본문 없음) |

## 시험

36건, 네트워크·실제 SDK 없이 돈다. 가짜 `openai` 모듈과 비밀 저장소를
`sys.modules` 에 넣어 환경변수·운영 비밀 경로를 모두 검증한다.

**돌연변이 10종 전부 잡힘**(생존 0) — 기본을 execute 로 바꾸기, 실패 시 한 번 더
부르기, 키 없이 진행, 모르는 provider 를 openai 로 보내기, 보낸 문장 출력, 벡터
출력, 오류 본문 출력, 비용 0 으로 만들기, 인자 검사 제거, 계획 모드에서 키 읽기.

## 아직 안 한 것

1. **진짜 호출은 한 번도 안 했다.** 이 문서의 목적이 그 한 번을 사람에게
   넘기는 것이다. 돌린 뒤 결과를 아래에 적어 주면 된다
2. **운영 비밀 경로는 기본이 아니다.** 기본은 환경변수다 — 스모크가 DB +
   암호화 키에 기대면 그 경로가 막힌 서버에서 **도구 자체가 못 돌고**, 그러면
   정작 보려던 응답 모양을 못 본다. 운영 경로까지 보려면
   `--key-from secret-store` 를 명시한다
3. **단가 출처가 여전히 없다.** 레지스트리의 `input_price_per_mtok` 를 읽지
   않는다(레지스트리를 안 보는 도구다). 비용을 보려면 `--price-per-mtok` 로 준다

## 돌린 결과 (사람이 채운다)

| | 값 |
|---|---|
| 날짜·사람 | *(미기입)* |
| provider · model | *(미기입)* |
| `vectors_returned` / `vector_widths` | *(미기입)* |
| usage 보고 여부 | *(미기입)* |
| 실패했다면 코드 | *(미기입)* |
