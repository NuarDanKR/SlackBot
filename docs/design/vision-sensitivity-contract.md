# Vision 전송 민감도 계약 — B-81(사진) · B-80(PDF 페이지) 공통

- 상태: 구현 (2026-11-19, Claude) · 커밋 전 오너 검토 대기
- 코드: [`src/tybot/archive/vision_sensitivity.py`](../../src/tybot/archive/vision_sensitivity.py)
- 시험: `tests/test_vision_sensitivity.py`, `tests/test_image_description_sensitivity_integration.py`

## 오너 결정 (2026-11-19)

> 당사는 일반 업무 문서를 별도 기밀로 분류하지 않으므로 Google Vision 전송을 허용한다.
> 따라서 일반 업무 이미지·PDF는 정책/catalog 민감도 불일치로 차단하지 않는다.
>
> 단, 다음 방어는 절대 완화하지 않는다.
> - PII 및 민감 문서 차단
> - OCR 불완전 상태에서 안전하다고 추정하지 않기
> - 원본 불변
> - LLM 결과는 파생 전사
> - 페이지별 중복 호출·재청구 방지
> - provider 오류 시 자동 재시도 금지

### B-81 이미지 예외 (2026-11-20 오너 결정)

Archiver가 정본으로 수집한 PNG/JPEG/WebP 이미지는 사진 속 PII의 외부 전송 전
판별·가림이나 파일별 clearance/approval 없이, 운영자가 켠 Vision 정책에 따라 승인된
Google 모델로 전송할 수 있다. 아래 표의 `pii_blocked` 사전 판정은 **B-80 PDF 페이지와
그 밖의 문서에는 유지**되지만 B-81 이미지 worker의 호출 조건에서는 제거한다.

폐지한 것은 전송 전 PII 관문뿐이다. 현재 ACL·정본 좌표와 원본 해시·봇 출력 제외·무료
모델 및 quota 확인·원본 불변·중복 호출 방지는 유지한다. 반환된 설명·OCR 텍스트는 저장과
전달 전에 기존 마스커와 출력 가드를 통과해야 하며 얼굴 신원 식별과 차량번호 전사는
계속 금지한다. 자동 처리를 `pii_cleared=true`인 사람 검토처럼 기록해서는 안 된다.

## 왜 바꾸나

전에는 정책이 `internal` 이고 모델 카탈로그가 `public` 이면 `sensitivity_not_allowed` 로
막혔다. 두 값은 같은 「일반 업무 문서」 를 서로 다른 말로 적은 것뿐인데, 사람이 둘을 맞추지
않으면 기능 전체가 오류 없이 멈췄다. B-80 은 아예 민감도 개념이 없이 `confidential` 로
고정돼 있어, 운영 Router 를 정책 값으로 만들면 모든 호출이 막혔을 것이다(B-81 은 f86542f 에서
같은 문제를 고쳤다).

## 계약

| 표기 | 계약 단계 | Google Vision |
|---|---|---|
| `public` · `internal` | 일반 업무 (`GENERAL_BUSINESS` = `internal`) | 허용 |
| `confidential` | 기밀 | 카탈로그가 기밀까지 승인한 경우만 |

- 정책·카탈로그 비교는 `catalog_allows(catalog_max, policy)` 하나로 한다. B-81 은
  `image_model_catalog.evaluate_free_vision_model`, B-80 은 `vision_policy.preflight` 가 부른다
- Router·provider 에 넘기는 값은 `request_sensitivity(policy)` 다. 운영 Router 의 모델 상한
  (`image_description_job._router`)도 같은 값이라 둘이 갈리지 않는다
- **넓히지 않는다.** 일반 업무가 기밀로 올라가거나 기밀이 일반으로 내려가는 변환은 없다
- 민감도를 빠뜨린 호출부는 기본값 `confidential` 로 일반 업무 Router 에서 **닫힌다**
- B-80 은 카탈로그 민감도(`checks['catalog_max_sensitivity']`)를 모르면 `catalog_sensitivity_unverified`
  로 막는다. 정책 `sensitivity` 가 없으면 일반 업무, 틀린 값이면 `policy_invalid`

## 그대로인 방어(완화하지 않음)

| 방어 | 자리 |
|---|---|
| PII·민감 문서 | B-80 문서에는 `pii_blocked` 사전 판정·격리를 유지한다. B-81 이미지는 2026-11-20 예외로 전송 전 판정을 하지 않고 반환 텍스트의 `pii_screen`·마스킹만 유지한다 |
| OCR 불완전 ≠ 안전 | 로컬 OCR 실패·부분 성공은 전송 승인도 PII 안전 증명도 아니다(B-80) |
| 원본 불변 · 파생 전사 | 결과는 별도 revision. 원문 MD 를 고치지 않는다 |
| 중복 호출·재청구 | 원장 `identity_sha256` UNIQUE(B-80 `archiver_vision_job`, B-81 `archiver_image_description_job`) |
| 자동 재시도 금지 | provider 오류·timeout 뒤 `unknown`/`failed` 로 닫고 다시 보내지 않는다 |
