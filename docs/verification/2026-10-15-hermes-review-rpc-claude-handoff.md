# PF Hermes 요약 검토 RPC 인계

Written for: Claude (Hermes worker 담당). 작성: Codex, 2026-10-15.

## 목적과 운영 증거

오너 승인: 예약 요약 검토도 PF Hermes가 후보를 생성하게 한다. 정상적으로 후보가
없을 때에도 검토자는 TYBot DM으로 결과를 받아야 한다. 장애를 정상 0건으로 숨기지 않는다.
토픽별 누적 저장소는 B-77로 분리했으며 이번 구현 범위가 아니다.

2026-10-14 TYIT 예약 검토는 TYBot `daily_review`에서 Haiku를 직접 호출했다.
15:03 회차는 HTTP 200, 출력 13토큰, 승인 가능 후보 0건이며 DM을 시도하지 않았다.
빈 모델 목록인지 검증 탈락인지는 당시 로그로 확정할 수 없다. 같은 날 이후 예약
회차는 `generated_on` 때문에 재생성하지 않았다. 공유 Hermes Q&A 성공과 별개다.

## 담당 경계

| 담당 | 수정 범위 |
|---|---|
| Claude | Node RPC 후보 생성 endpoint, 검토 프롬프트, worker 계약·취소·공유 격리 시험 |
| Codex | Python 검토 RPC client/배선, 원문 검증, 증분 재시도, 상태 DM·영속 멱등 기록, 콘솔 인계 |

Claude는 `src/tybot/`, `deploy/sql/`, `BACKLOG.md`, `docs/web-console-handoff.md`를
수정하지 않는다. Node 공용 server/wire/provider 변경은 기존 Q&A 회귀를 반드시 검증한다.
커밋·push는 변경 범위를 확인한 뒤 별도로 보고한다. 개발 PC 테스트를 서버 검증으로
표시하지 않는다. worker 구현은 `50b4afd`로 완료됐고 회신 문서에 별도 기록했다.
아래는 착수 당시 계약이며 최신 Python 상태는 맨 아래를 우선한다.

## 요청 계약 (이번 인계의 기준)

같은 Unix socket의 `POST /v1/review-candidates`를 추가한다. 새 TCP 포트는 없다.
요청 schema는 `tybot.hermes.review.request.v1`, 응답은 `tybot.hermes.review.response.v1`.
Q&A의 `tybot.specialist.*.v3` schema를 바꾸거나 질문 문자열로 작업을 추측하지 않는다.

요청 필드:

- `schema`, UUID `request_id`, 실제 `workspace` key
- `source_digest`: TYBot이 고른 원문 구간의 SHA-256 (64자리 소문자 hex)
- `sources`: `{id, at, author, text}` 배열. `id`는 `r1`부터의 요청 내 유일한 ID
- `approved`: 기존 승인 문장 문자열 배열. 수정 대상 대조용이며 새 사실의 근거가 아니다.
- `limits`: `deadline_ms` (최대 180000), `max_candidates` (1~10)

TYBot이 이미 선정한 단일 채널의 사람 원문만 받는다. 파일 경로·Slack 토큰·임의
프롬프트·provider key·도구 socket은 요청에서 받지 않는다. `sources`가 비거나 ID가
중복/형식 오류이면 400이다. 요청 본문은 최대 256 KiB, 원문 text 합계 31500자,
승인 문장 합계 8000자, 원문 항목 최대 1000개로 제한한다. TYBot도 같은 상한을 쓴다.
도구 토큰/도구 호출이 없는 독립 작업이며 Slack·아카이브 직접 조회는 계속 금지한다.

응답 필드:

- `schema`, `request_id`, `workspace`, `source_digest` (요청값과 정확히 일치)
- `status`: `ok` / `empty` / `failed`
- `candidates`: 기존 `contract/summary-review.md`의 후보 필드 + `source_id`
- `error_code`: 정상은 빈 문자열, 실패는 allowlist 코드 (본문/시크릿 포함 금지)
- `metrics`: 모델명, 입출력 토큰, stop_reason, elapsed_ms

metrics의 공통 키는 `model`, `input_tokens`, `output_tokens`, `stop_reason`,
`elapsed_ms`, `proposed_count`, `accepted_count`, `rejected_counts`로 고정한다.
`rejected_counts`는 사유 코드별 정수 건수 객체이며 본문을 담지 않는다.

`ok`는 후보 1~10개, `empty`는 모델 호출·출력 형식 검증을 성공하고 후보가 0개일 때만.
timeout, invalid JSON, truncated output, provider 오류, workspace 미등록/키 누락을
`empty`로 바꾸지 않는다. `failed`에는 후보를 싣지 않는다. 미등록 workspace는 기존과
동일하게 거절한다. 승인 본문만으로 새 후보를 만들지 않는다.

모델은 각 후보의 `source_id`와 `evidence_quote`를 함께 출력한다. Node에서도 해당
원문과 대조하고, Python은 반드시 독립적으로 재검증한다. 원문 좌표·해시·채널 ID는
TYBot이 보관한 원문에서만 결합한다. 후보가 검증 탈락하면 정상적인 `empty`와
구분할 수 있게 제안/탈락 건수와 이유 코드만 보고한다. 모델이 후보를 냈지만 전부
탈락하면 `failed` / `candidate_validation_failed`이며 `empty`가 아니다.
숫자·현재 승인 문장 대조,
quote/abstract 규칙은 현재 계약보다 느슨하게 만들지 않는다.

요청 HMAC/nonce/replay와 응답 서명은 기존 함수·정규화를 재사용하되 endpoint 경로를
서명에 포함한다. 응답도 request_id에 결합한다. Q&A와 검토 간 request_id 중복을
거절하고 취소 endpoint를 공유한다. 단일·공유 worker 모두 지원한다.

기존 전체/워크스페이스 동시 상한을 Q&A와 공유한다. 검토 호출용 별도 슬롯 풀을
만들어 합산 상한을 넘기지 않는다. 모델 호출에 AbortSignal을 전달하고 실제 settle
전 슬롯 반환 금지. 429는 기존처럼 request_id를 점유하지 않는다.

health 추가 필드 `capabilities: ["review_candidates_v1"]`로 준비 여부를 알린다.
TYBot은 미지원 worker를 Haiku로 조용히 우회하지 않고 설정/계약 오류로 진단한다.
workspace별 profile의 provider key와 모델 설정을 사용하며 process.env 변조 금지.
완료 로그에는 workspace, request_id, status, 모델·건수·시간만 남긴다. 원문·후보
문장·승인 본문·API key를 로그에 남기지 않는다.

## Codex가 맡을 동작 (Claude 구현 아님)

- 모델 빈 목록 / 원문 없음 / 검증 탈락 / 백오프 / 이미 처리 / 생성 장애를 구분한다.
- 새 원문 digest가 있으면 같은 날에도 재검토하되, 동일 digest 중복·재시도 간격·
  workspace 비용 예산·동시 실행 잠금으로 호출을 제한한다. 커서 강제 초기화 금지.
- 발송 시각 이후 활성 검토자에게 정상 0건 상태 DM도 채널별 하루 한 번 보낸다.
  나중에 후보가 생기면 정규 검토 DM을 따로 받을 수 있어야 한다.
- 상태 DM 이력을 후보 발송/승인 권한 이력과 섞지 않는다. 승인 버튼·Canvas 없음.
  재시작/타이머 중복에 견디는 영속 기록, 발송 실패·응답 유실 구분이 필요하다.
- 생성/검증 장애를 "요약할 내용 없음"으로 알리지 않는다. 장애 DM은 별도 사유와
  중복 제한을 두며, 생성 시스템이 죽은 경우의 운영 경고 경로도 구분한다.
- Master 없는 TEC는 Archiver/Hermes만으로 수집 가능하다는 기존 계약을 유지한다.
  TYBot DM sender 부재를 수집 실패나 정상 0건으로 처리하지 않는다. 별도 발송 신원
  지원 여부는 오너 결정/후속 계약이며 Slack 토큰을 RPC worker에 추가하지 않는다.

## 필수 시험과 회신

정상/빈 결과/잘린 출력/파싱 실패/provider 오류/timeout/취소, HMAC·replay·응답서명,
요청 상한·후보 상한·존재하지 않는 source_id·교차 workspace 근거·승인문장만 인용,
공유 workspace profile 격리, Q&A와 검토 동시 상한, 늦은 settle, 미지원 capability,
기존 Q&A 단일/공유 회귀를 검증한다. body나 key가 로그에 없는지도 확인한다.

회신은 이 문서 옆 `2026-10-15-hermes-review-rpc-claude-handback.md`에 작성한다.
변경 파일·실제 JSON 예시(가상 원문)·시험 결과·못 잰 Linux 항목·커밋을 적는다.
계약 변경이 필요하면 Python 구현 전에 변경점을 먼저 공유한다.

## Codex 독립 작업 상태 (2026-10-15)

Python의 빈 결과/검증 탈락/백오프 진단, 상태 DM 별도 영속 이력, 같은 날 15분 간격
재검토와 CLI 단일 실행 잠금을 구현했다. 상태 DM 테이블은
`deploy/sql/summary_review_schema.sql`에 추가했다. Node 파일은 수정하지 않았다.
전체 pytest 4522 passed / 71 skipped / 7 warnings(Windows guard subprocess 디코딩),
이후 최신 검토 관련 시험 179 passed, Ruff 통과. 서버 신규 테이블/권한·실제 DM은
아직 확인하지 않았다. Python RPC client/실제 생성 배선은 worker 회신을 받고 연결한다.
초기 독립 작업 당시에는 후보 생성이 기존 게이트웨이 경로였으며, 이후 회신을 받아
Python RPC 연결을 추가했다. 아래 최신 상태를 우선한다.

## Codex 연결 후 상태 (2026-10-15, 미배포)

- `gateway/hermes_review.py`: 기존 HMAC credential·endpoint 정책 재사용. health capability,
  실제 workspace 준비 상태, 응답 서명, request/workspace/source_digest 결합 검증.
- `daily_review`: 별도 `TYBOT_HERMES_REVIEW_WORKSPACES` 허용 workspace는 단일/공유 PF RPC로
  예약·소급 후보 생성. 기본 OFF이며 Master v2 허용 목록의 부분집합만 받는다.
  미지원/실패 시 Haiku로 조용히 우회하지 않는다. 비적용 workspace만 기존 경로 유지.
- Python은 `source_id`로 정확한 원문 한 줄을 다시 대조하며 좌표·해시·시각·작성자를
  로컬 원문에서 결합한다. 숫자/승인 문장 베끼기/잘못된 form도 독립 거절한다.
- 원문 없음은 RPC 호출 없이 상태 DM, 모델 정상 0건은 empty, 생성 장애·검증 탈락은
  오류 상태 DM이다. 실패 시 커서를 넘기지 않고 재시도 대기를 기록한다.
- gateway 예산을 호출 전에 확인한다. 미등록 모델 단가 또는 응답 유실 사용량은
  보수적인 추정값으로 집계한다(실제 청구액 아님). 운영 단가 등록 확인이 필요하다.
- Node review 43건·Python worker 계약 17건·관련 최신 Python 185건과 Ruff 통과.
  직전 전체 pytest 4587 passed / 72 skipped / 7 warnings(Windows guard 인코딩)이며
  이후 rollout 기본 OFF 추가 검사는 관련 시험에서 통과했다.
  실제 Linux 소켓/credential/모델/발송은 아직 검증하지 않았다. 커밋·배포 전이다.
