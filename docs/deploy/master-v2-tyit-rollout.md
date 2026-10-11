# TYIT LLM 마스터·외부 Hermes 단계 배포

상태: 구현 완료, Node Hermes v3 worker 합류 전에는 활성화 금지  
범위: 전산팀(`tyit`) 한 곳만

## 기본 상태

코드 배포만으로 동작은 바뀌지 않는다. `TYBOT_MASTER_V2_WORKSPACES`가 비어 있으면
기존 워크스페이스는 새 route 조회, HMAC 파일, Unix callback socket에 의존하지 않는다.
`*`는 허용하지 않으며 워크스페이스 키를 명시적으로 열거해야 한다.

## 활성화 전 조건

1. Claude 작업의 Node Hermes worker가
   `/run/tybot-subbots/hermes-tyit/http.sock`에서 v3 계약으로 응답한다.
2. TYBot과 `hermes-tyit` 사용자가 `tybot-specialist-rpc` 보조 그룹에 속한다.
3. 양쪽 서비스가 같은 HMAC credential을 각자 읽을 수 있는 파일로 받는다. 값을 env에
   직접 넣지 않는다. credential은 전문봇·워크스페이스마다 다르다.
4. `specialist_route`의 `hermes/tyit` 행만 `active`이고 다른 워크스페이스는 바꾸지 않는다.
5. Hermes의 `/v3/answer`, TYBot의 `/v3/tools/call`, 응답 서명 smoke test가 통과한다.

## 마지막 활성화

`/etc/tybot/tybot.env`에는 아래 두 값만 추가한다. credential 디렉터리는 TYBot만
탐색할 수 있어야 한다.

```dotenv
TYBOT_MASTER_V2_WORKSPACES=tyit
TYBOT_SPECIALIST_V3_HMAC_DIR=/etc/tybot/credentials/specialist-v3
```

TYBot이 읽는 파일명은 `<specialist>-<workspace>.hmac`으로 고정한다. 첫 파일은
`/etc/tybot/credentials/specialist-v3/hermes-tyit.hmac`이다. Hermes worker의
`LoadCredential` 원본과 **같은 바이트**여야 하지만 파일 소유권은 분리한다. TYBot 쪽은
`root:tybot 0440`, worker 쪽은 `root:hermes-rpc-tyit 0640`으로 두며 한 파일의 그룹을
여러 서비스에 열지 않는다.

확산할 때는 `hermes-pilot.hmac`처럼 인스턴스별 파일을 추가한다. 전문봇이 없는
워크스페이스는 Hermes credential 파일이 필요하지 않다.

그 뒤 TYBot을 재시작한다. 쉼표로 다른 키를 추가하지 않는 한 `invest`, `archi`,
`civil`, `mgmt`, `pilot`은 기존 경로를 유지한다.

## 판정 규칙

- LLM이 `specialist: "hermes"`를 선택하고 active route가 있을 때만 외부 Hermes 호출
- LLM이 specialist를 비우거나 맞는 active specialist가 없으면 TYBot 직접 답변
- planner 장애·잘못된 JSON, route DB 장애, Hermes timeout·서명 위반은 직접 답변으로
  숨기지 않고 실패로 닫음
- 직접 답변과 Hermes 답변 모두 현재 요청자의 ACL을 통과한 근거와 출처가 필수

## 즉시 되돌리기

`TYBOT_MASTER_V2_WORKSPACES`를 공란으로 바꾸고 TYBot만 재시작한다. route 행과 Hermes
직접 Slack 앱은 이 롤백 때문에 수정하지 않는다. 실패 원인을 보존한 채 기존 경로로
돌아가기 위한 스위치다.
