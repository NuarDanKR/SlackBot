# Hermes 외부 전문봇 RPC - Claude 작업 인계

날짜: 2026-10-08  
상위 작업: B-76  
담당 경계: Claude는 `subbots/hermes/**`와 Hermes 전용 배치 파일만 수정한다.

## 목표

TYBot 안의 Python `ToolSpecialist`를 재사용하지 않는다. 수정된 Node.js Hermes가
전문봇 요청을 받아 답변과 실제 사용한 근거 ID를 반환하는 worker 진입점을 만든다.
TYBot은 최종 Slack 발송자이며 RPC 요청에 대해 Hermes가 Slack 메시지를 보내면 안 된다.

## 구현 계약

1. 외부 TCP 포트를 열지 않는다. Unix socket을 사용한다.
2. 워크스페이스별 소켓 경로를 사용한다. 첫 대상은
   `/run/tybot-subbots/hermes-tyit/http.sock`이다.
   Hermes가 TYBot 도구를 호출하는 반대 방향은
   `/run/tybot/specialist-tools.sock`이다. 두 프로세스가 같은 경로에서 동시에
   listen할 수 없으므로 요청 방향별 소켓을 혼용하지 않는다.
3. `src/tybot/specialist_wire_v3.py`와
   `docs/design/specialist-v3-tool-contract.md`의 스키마를 기준으로 구현한다.
4. 요청 서명·nonce·시각 검증은 `specialist_wire.py`와 호환돼야 한다. HMAC은
   `(specialist, workspace)` 인스턴스마다 다르며 다른 인스턴스 요청에 쓸 수 없다.
5. 요청의 `workspace`, `authorization_id`, `request_id`, deadline을 다른 요청과
   섞지 않는다.
6. 자료는 v3 도구 호출로만 받는다. worker에 Archiver 전체 경로 또는 Slack 토큰을
   추가로 주지 않는다.
7. `answered` 응답에는 TYBot이 발급한 `used_evidence`가 하나 이상 있어야 한다.
8. `no_evidence`, `refused`, `failed`를 구분하고 자유 형식 오류를 반환하지 않는다.
9. 직접 Slack `@Hermes` 경로와 답변 코어는 공유해도 되지만 RPC worker는 Slack에
   직접 게시하지 않는다.
10. Socket Mode의 ping/pong을 막지 않도록 RPC worker는 별도 프로세스로 실행한다.
11. worker 서비스 계정은 `tybot-specialist-rpc` 보조 그룹에만 추가한다. TYBot의
    기본 그룹을 공유하지 않는다. 그래야 `/run/tybot/specialist-tools.sock`에는
    접근하면서 아카이브 파일 권한을 함께 얻지 않는다.

## Claude가 수정하지 않을 파일

- `src/tybot/intent.py`
- `src/tybot/master_planner.py`
- `src/tybot/specialist_router.py`
- `src/tybot/slack/pilot.py`
- `src/tybot/answer.py`
- `tests/test_master_v2_rollout.py`

이 파일들은 Codex가 LLM 라우팅, workspace rollout, Python RPC 클라이언트와 마스터
폴백을 구현한다. 공통 계약 변경이 필요하면 먼저 이 문서에 제안만 남긴다.

## 검증 산출물

- Node 오프라인 계약 시험: 정상 답변, 근거 없음, 잘못된 schema/request ID,
  만료 서명, nonce 재사용, deadline, 출력 상한
- worker health 시험은 모델을 호출하지 않고 2초 안에 완료
- RPC 요청 중 Slack `chat.postMessage`가 0회임을 확인하는 시험
- systemd unit은 workspace별 사용자와 socket 디렉터리를 분리하고 기본 disabled
- 실제 토큰·API 키·사내 원문을 fixture나 로그에 넣지 않음

## 완료 후 Codex에 전달할 정보

- 실행 명령과 systemd unit 이름
- socket 경로
- HMAC secret을 읽는 credential/env 이름
- 구현한 wire schema/version
- health 응답 예시(시크릿 제외)
- Node 시험 명령과 결과
