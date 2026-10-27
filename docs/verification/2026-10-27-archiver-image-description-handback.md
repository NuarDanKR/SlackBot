# B-81 이미지 설명 코어 — Claude 인계 (2026-10-27)

분담: [`2026-10-27-archiver-image-description-handoff.md`](2026-10-27-archiver-image-description-handoff.md)

신규 두 파일만 만들었다. 수정 금지 파일은 건드리지 않았다.

| 파일 | 내용 |
|---|---|
| `src/tybot/archive/image_description.py` | 순수 코어 |
| `tests/test_image_description.py` | fake router 시험 69건 |

## 계약

```python
describe_image(
    image: ImageInput,          # 승인된 bytes + 좌표
    *, router, model: str, provider: str,
    max_output_tokens: int, timeout_seconds: float,
) -> ImageDescription
```

DB·env·파일·provider SDK 를 **import 하지 않는다.** 바깥에서 가져오는 것은
`gateway.base` 의 `Message`·`Sensitivity` 뿐이고 그마저 함수 안에서 늦게 받는다.

### 입력 `ImageInput`

`workspace` · `channel` · `file_id` · `original_sha256` · `image` · `mime`.
경로·URL·토큰을 받지 않는다. **모델에게 좌표를 보내지 않는다** — 보내지 않으면
모델이 적어 보낼 좌표가 없고, 대조할 일도 없다.

### 출력 `ImageDescription`

| 상태 | 뜻 |
|---|---|
| `described` | 검수 가능한 짧은 설명이 있고 불확실성 코드가 없다 |
| `uncertain` | 설명은 있으나 코드가 붙었거나, 모델이 이유를 대고 못 적었다 |
| `failed` | 모델 오류·시간 초과·잘림·빈 출력·깨진 구조·identity 불일치·상한 초과 |

같이 나가는 것: `description`(≤200자) · `tags`(허용 목록) ·
`uncertainty`(고정 코드) · `error_code` · `usage` · `prompt_revision`.

`usage` 는 **실제** model/provider·토큰·비용·종료 사유다. **모르면 `None`** —
0 으로 적으면 무료 quota 집계가 조용히 틀린다. 토큰을 모르면 비용도 `None` 이다.

## 설계에서 바꿀 수 있는 자리

### 자유 confidence 숫자를 안 받는다

`confidence: 0.82` 는 모델마다·호출마다 판이 달라 **비교할 수 없다.** 그 숫자로
임계값을 세우면 임계값이 뜻을 잃는다. 대신 고정 코드 열 개를 쓴다.

앞 다섯(`low_resolution`·`blurred`·`too_dark`·`overexposed`·`occluded`)은
**사진이 나쁜** 경우다. 뒤 다섯(`person_visible`·`plate_visible`·`text_heavy`·
`measurement_visible`·`damage_like_subject`)은 **우리가 판단을 금지한 것이
보이는** 경우다. 둘 다 `uncertain` 이지만 사람이 할 일이 다르다 — 앞은 다시
찍어야 하고, 뒤는 사람이 봐야 한다. 콘솔에서 가를 수 있게 코드를 나눴다.

### 태그는 허용 목록뿐

자유 태그를 허용하면 두 가지가 같이 깨진다. 검색에서는 같은 대상이 매번 다른
말로 적혀 묶이지 않고, 안전에서는 **사진 속 글자가 태그 자리로 흘러나온다** —
문서를 찍은 사진이면 그 본문이 색인에 들어간다. 목록 밖은 버린다.

`인물포함` 은 넣었다. **있다는 것까지만**이고 누구인지는 적지 않는다. PII 검토가
필요한 사진을 찾는 데 쓰인다.

### 길면 자르지 않고 **버린다**

설명이 200자를 넘으면 `failed`(`output_too_long`)다. 두 가지 이유다.

1. 길이 규칙을 안 지킨 응답에서 **다른 규칙도 지켜졌다고 볼 근거가 없다**
2. 설명 자리는 사진 속 문서 본문이 흘러나오는 길이다 — 길면 자르는 것이 아니라
   버리는 것이 맞다

### `describable: false` 는 실패가 아니다

「너무 어두워 범주조차 못 적겠다」 는 모델 고장이 아니라 **대답**이다.
이유 코드가 있으면 `uncertain` 으로 남긴다. 이유가 없으면 아무것도 알려 주지
않은 것이라 `failed`(`invalid_output`)다.

### 종료 사유가 형식 판정보다 먼저다

`stop_reason == "max_tokens"` 면 괄호가 맞아도 `truncated_output` 이다. 상한에
걸려 끊긴 문장을 완성본으로 받으면 **반쪽짜리 설명이 색인에 들어간다.**

### identity 를 두 번 본다

전송 전에는 `restricted_to` 로 폴백 없는 라우터를 만들고, 전송 후에는 응답의
model/provider 를 다시 본다. 빈 영수증은 승인 모델이 처리했다는 증거가 아니다.
어긋나면 `model_not_approved` 로 닫되 **실제로 쓴 값은 `usage` 에 남긴다.**

무료 모델이 막히면 `ImageInputError` 로 **호출 전에** 닫는다. 유료로 넘어가지
않는다 — 넘어가면 「무료라서 켠 기능」 이 조용히 과금된다.

### 사진 안의 글자는 데이터

사용자 메시지는 **이미지 블록이 먼저, 우리 지시가 마지막**이다. 사진에 지시처럼
생긴 글자가 있어도 마지막 말이 우리 것이다. 프롬프트도 같은 말을 한다.

주입이 성공해도 **새 태그를 만들 수는 없다** — 목록 밖은 버려지고, 호출이
늘지도 않는다(사진당 한 번 고정).

## 시험 69건

개발 PC(Windows)에서 `pytest tests/test_image_description.py` — **69 passed**.
서버에서 돌릴 일이 없는 순수 단위 시험이다(네트워크·DB·파일을 쓰지 않는다).

요구된 항목 전부 포함: 정상 · uncertain · 빈 결과 · truncated · 깨진 JSON ·
model/provider 불일치 · 이미지 지시문 주입 · 호출 1회 · 상한 전달 ·
본문 로그 금지.

실제 사진·API 키·네트워크를 쓰지 않는다. 합성 바이트와 라우터 대역뿐이다.

### 돌연변이 검사

지키려는 것이 실제로 물리는지 하나씩 되돌려 확인했다.

| 되돌린 것 | red |
|---|---|
| identity 검사 제거 | 3건 |
| 상한 초과를 자르기 | 1건 |
| 태그 허용 목록 미적용 | 2건 |
| 설명을 로그에 기록 | 2건 |
| 종료 사유 무시 | 2건 |
| 이유 없는 `describable:false` 허용 | 1건 |

## Codex 쪽에서 해야 하는 것

이 코어는 **의도적으로 모른다.** 아래는 전부 원장·worker 몫이다.

- 재시도·폴백·동시성. 코어는 **사진당 한 번**만 부르고 끝난다
- 정책 OFF·ACL·PII·봇 생성 파일·해시 변경 확인. 코어는 **이미 승인된** bytes 를 받는다
- 무료 quota 소진 판정(`image_model_catalog`). 코어는 `router.resolve` 실패를
  호출 전 거절로 바꿀 뿐이다
- `prompt_revision` · 원본 SHA · 실제 모델을 묶어 저장
- 검색에서 **후보 발견에만** 쓰고 답변 근거는 원본 사진 좌표로 연결

### 계약 변경 요청 없음

인계에 적힌 입력·출력으로 그대로 구현했다. 한 가지만 **더했다**:
결과에 `prompt_revision` 을 실었다. 프롬프트를 소유한 쪽이 코어라 저장할 판을
코어가 주는 편이 맞고, 안 주면 worker 가 손으로 적어 두 값이 어긋난다.

## 미검증

- **실제 모델 품질.** 모델이 금지 항목을 실제로 안 쓰는지는 재지 않았다.
  코드가 막는 것은 **형식**(태그 목록·길이·코드 목록)이고, 설명 문장 안에
  사람 이름이나 수량이 들어오는 것은 **프롬프트에만 의지한다.** 합성 비민감
  이미지로 사람이 읽어 보는 단계가 활성화 전에 필요하다(인계 §활성화 전)
- **이미지 바이트 크기 상한이 없다.** `vision_extract` 와 맞춘 것인데, 큰 사진이
  오면 요청이 커진다. 상한은 worker 정책이 맞다고 보지만 **지금은 어디에도 없다**
- `image/webp` 를 허용 목록에 넣었으나 gateway·provider 가 실제로 받는지는
  **이 저장소에서 확인하지 못했다.** 현재 `anthropic_provider` 는 media_type 을
  그대로 넘긴다
- 비용 `0.0` 과 `None` 의 구분은 단위 시험으로만 봤다. 무료 모델이 실제로 `0.0`
  을 돌려주는지는 Codex 카탈로그 쪽 운영 확인이 필요하다
