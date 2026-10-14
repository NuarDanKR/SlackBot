# 공유 Hermes RPC worker - Claude 작업 인계

날짜: 2026-10-12. B-76 확장. 기존 TYIT 운영 검증 완료, 추가 확산 전 구현.

## 목적과 소유 범위

같은 호스트에서 여러 워크스페이스가 하나의 Node Hermes RPC worker를 사용한다.
자료 접근은 계속 TYBot의 요청별 ToolBox/ACL이 담당한다. 직접 Slack Hermes와
Archiver 운영에는 변경이 없다. 공유 프로세스가 여러 workspace credential을 읽는
것은 이번 공유 실행에 필요한 신뢰 경계 변경이며, 장애와 credential 노출 영향도
공유 대상 전체로 넓어진다. 워크스페이스별 OS 격리가 필요하면 기존 개별 worker를 쓴다.

- Codex: `src/tybot/**`, Python 시험, 중앙 계약/인계 문서.
- Claude: `subbots/hermes/**`의 worker, 설정, Node 시험, 별도 공유 unit/helper.
- `deploy/install.sh`와 Python 파일은 Claude가 수정하지 않는다.
- 환경변수 관리의 DB/API 전환은 웹콘솔 연계 후속 작업이다. 이번 작업에 섞지 않는다.

## 고정된 연결 계약

1. 스키마는 기존 v3 그대로다. 요청의 `workspace`는 실제 workspace key이며
   `shared`로 바꾸지 않는다. authorization/request ID, token, evidence도 그대로 유지한다.
2. 공유 받기 소켓: `/run/tybot-subbots/hermes-shared/http.sock`.
   도구 보내기 소켓: `/run/tybot/specialist-tools.sock` (기존 그대로).
3. TYBot 선택 설정: `TYBOT_HERMES_SHARED_WORKSPACES=tyit,mgmt`.
   기본 빈 목록, wildcard 금지. Master v2 allowlist의 부분집합이어야 한다.
   공유 목록 밖 workspace는 기존 `hermes-<ws>/http.sock`을 사용한다.
   공유 연결 실패 시 개별 worker나 내부 Python Hermes로 재시도하지 않는다.
4. TYBot HMAC: 기존 `TYBOT_SPECIALIST_V3_HMAC_DIR` 밑 `hermes-shared.hmac`.
   도구 서버 credential identity는 `hermes:shared` 하나다. 세션 권한은 identity가
   아니라 실제 요청에 묶인 ToolBox/token에서 결정한다. 기존 개별 HMAC과는 다른
   공유 전용 비밀을 새로 만든다. workspace별 HMAC 파일을 복제하는 방식은 쓰지 않는다.
5. 공유 계정 `hermes-rpc-shared`, 공유 service `hermes-rpc-shared.service`.
   worker 측 비밀 파일은 `/etc/hermes-rpc-shared/hmac`, 설정 파일은
   `/etc/hermes-rpc-shared/rpc.env`. Rocky 8/systemd 239에서 LoadCredential 없이
   HERMES_RPC_HMAC_FILE을 사용하고 기존 소켓 그룹 보정/권한 검사를 유지한다.
6. workspace allowlist는 worker의 서버 설정에서 가져온다. 클라이언트 본문으로
   model/domain/API key/파일 경로를 선택하지 않는다. 미등록 workspace는 거절한다.
7. health는 기존 서명과 `/v1/health`, `contract_version=v2` 계약을 유지한다.
   health는 모델을 호출하지 않는다. 전체 health와 workspace별 준비 상태는 구분한다.
8. 취소는 서명과 request ID를 검사하고 해당 요청만 취소한다. request ID 충돌은
   기존처럼 거절한다. 어느 workspace도 다른 요청 token/근거를 가져올 수 없다.

## Claude가 구현할 설정과 실행

공유 entrypoint는 별도로 추가한다. 기존 `npm run rpc`와 `hermes-rpc@.service`는
개별 모드로 유지하며 `shared`를 실제 workspace로 취급하지 않는다.

권장 공유 설정:

```text
HERMES_MODE=tybot
HERMES_RPC_HMAC_FILE=/etc/hermes-rpc-shared/hmac
HERMES_RPC_PROFILES_FILE=/etc/hermes-rpc-shared/profiles.json
HERMES_RPC_MAX_CONCURRENT=4
HERMES_RPC_MAX_PER_WORKSPACE=2
```

profiles.json은 서버 운영자가 관리하는 정적 매핑이다. key는 workspace, 각 값은
`domain`과 `api_key_file`, 선택적인 `model`/`effort`다. 비밀은 JSON에 직접 넣지 않고
계정이 읽을 수 있는 credential 파일에 둔다. 같은 API 키를 여러 profile에서 명시적으로
참조하는 것은 가능하다. 글로벌 fallback 키로 미등록 workspace를 허용하면 안 된다.
Node 설정 필드의 정확한 최종 모양은 handback에 기록한다.

- workspace마다 다른 domain/client를 사용한다. 공유 mutable 모델 대화는 금지한다.
- API 키는 요청별 client에 넣는다. 요청 중 process.env 변경은 금지한다.
- request별 tool client, issued evidence, trace, AbortController, 대화는 새로 만든다.
- 전체/워크스페이스별 동시 요청 상한을 강제한다. 초과는 HTTP 429로 즉시 반환하고
  무제한 대기열을 만들지 않는다. TYBot은 이를 실패로 닫으며 임의 fallback을 하지 않는다.
- 재시작은 전체에 영향을 주므로 TYIT만 먼저 공유 경로로 검증 후 순차 확대한다.
- 원문·검색어·질문·API 키·HMAC/token은 journal에 남기지 않는다. workspace key,
  request ID, 상태·도구 횟수·토큰 수·시간만 기록한다.

## 필수 시험과 회신

- TYIT와 MGMT 동시 요청에 서로 다른 합성 원문/근거를 주고 교차 인용을 거절한다.
- workspace별 domain/API client 선택, 미등록 workspace 거절.
- 한 요청 취소 후 다른 요청은 성공하며 token과 issued evidence를 섞지 않는다.
- 전체/워크스페이스 동시성 상한, 초과 429, 완료/오류/취소 후 슬롯 반환.
- 기존 개별 worker 시험과 공유 wire/HMAC/nonce/health/권한 시험 통과.
- RPC 도구가 발급한 [eN]을 모델에 전달하는 0956bec 수정을 유지한다.
- service 기본 disabled, enable/start는 운영자가 한다. env/비밀 자동 복제 금지.
- Linux 실제 소켓/계정 smoke는 서버에서 별도로 검증한다.

회신: 변경 파일, 실행 명령, 실제 설정 예시(비밀 없이), unit 설치 명령, 시험 결과,
미검증 항목을 `docs/verification/2026-10-12-hermes-shared-rpc-handback.md`에 기록한다.
상대 담당 파일은 수정하지 않고, 커밋은 자신의 변경만 골라서 한다.
