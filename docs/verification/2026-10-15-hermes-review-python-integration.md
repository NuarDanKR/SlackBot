# Hermes review RPC Python 연결 검증

담당 Codex · 2026-10-15 · 로컬 구현/검증, 서버 적용 전.

## 범위

Claude `50b4afd`의 endpoint/계약은 변경하지 않았다. Node review 시험 43건과
`tests/test_hermes_rpc_contract.py` 17건을 독립 실행해 통과했다.
Python 예약·소급 생성은 기존 source 선정/커서/후보 DB/승인·Canvas 전달을 유지하며
생성 부분만 `gateway.hermes_review.ReviewGenerator`를 통해 호출한다.

`TYBOT_HERMES_REVIEW_WORKSPACES` 대상은 PF RPC, 나머지는 명시적인 기존 경로다.
기본 OFF이며 `TYBOT_MASTER_V2_WORKSPACES`의 부분집합만 허용한다. Q&A rollout과 별개다.
공유 여부는 `TYBOT_HERMES_SHARED_WORKSPACES`와 기존 endpoint 정책을 재사용한다.
공유 credential을 사용해도 요청 workspace는 실제 workspace다. 별도 포트는 없다.
RPC 선택 후의 장애를 내부 모델로 우회하지 않는다.

## 검증 경계

- 요청은 한 채널의 선정된 사람 원문이며 경로·Slack 토큰·도구 권한을 전달하지 않는다.
- 항목 1000개/원문 31500 UTF-16 단위/승인 대조문 8000단위/요청 256KiB 제한.
- health의 capability·workspace readiness 후 요청을 서명하고, 응답 서명을 먼저 검증.
- schema/request_id/workspace/source_digest/상태/후보 수/통계 allowlist를 대조한다.
- source_id로 로컬 원문을 찾아 인용을 검증한다. worker가 준 좌표·해시·작성자는 쓰지 않는다.
- 인용 밖 숫자/승인본만 베낀 후보/알 수 없는 form/다른 원문의 인용을 거절한다.
- timeout이면 공유 cancel 경로로 취소한다. 실패·전부 탈락 시 커서를 넘기지 않는다.
- 정상 0건/원문 없음/생성 실패 상태 DM은 별도 영속 이력으로 하루 중복을 제한한다.
  이후 실제 후보 발송을 막지 않으며 승인 권한을 부여하지 않는다.

## 비용 제한

gateway의 기존 전체/workspace 비용 가드를 사용한다. 호출 전 기존 confidential
모델 단가표 중 보수적인 견적을 확인하고, 응답 모델이 단가표에 있으면 보고 토큰으로
계산한다. **단가 미등록 모델 또는 응답 유실은 추정 비용을 기록**한다. 실제 청구액이
아니며 일일 한도가 보수적으로 차단될 수 있다. 운영 모델의 단가표 반영은 별도 확인한다.
기존 비용 상태 저장소의 프로세스 간 집계 제약을 해결한 변경은 아니다.

## 서버에서 남은 확인 순서

1. 코드 배포 시 `summary_review_status_delivery` 테이블/권한을 적용한다.
2. shared worker 재시작은 전체 대상에 영향을 주므로 진행 중 Q&A를 확인하고 시행한다.
3. Claude handback §13의 TYBot credential 서명 health로 capability와 workspace를 확인한다.
4. `TYBOT_HERMES_REVIEW_WORKSPACES=tyit`을 검증 프로세스에 명시하고,
   `daily_review --help`에서 CLI 인자를 확인해 TYIT 한 채널만 generate-only로 실행한다.
   예약일 커서 강제 초기화나 전체 소급을 검증 수단으로 사용하지 않는다.
5. worker의 `[rpc] 검토 완료`와 TYBot `review_rpc` 로그 request/workspace를 대조한다.
6. 근거 좌표·후보 저장·Canvas/DM·0건/오류 상태 DM·재실행 중복 방지를 확인한다.
7. 확인한 뒤 다른 workspace로 확산한다. Master 없는 TEC의 sender 지원은 별도다.

관련 최신 Python 시험 185건, Ruff 통과. 직전 전체 pytest는 4587 passed / 72 skipped /
7 warnings(Windows guard 인코딩). 이후 rollout 기본 OFF 추가 검사는 관련 시험에서 통과했다.
실제 모델 호출/DB 신규 권한/실제 Slack 발송은 로컬 시험으로 검증했다고 표시하지 않는다.
