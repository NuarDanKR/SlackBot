# 공유 Hermes RPC - Codex 통합 리뷰

## 2026-10-14 재검증 - 보완 완료

Claude 커밋 `5aab006`에서 아래 P1/P2 세 항목을 보완한 것을 확인했다.
AbortSignal이 모델 SDK와 도구 HTTP까지 전달되며 서버는 실제 loop의 settled 이후
슬롯을 반환한다. timeout/취소의 느린 teardown에서도 상한이 유지되는 시험을 확인했다.
health smoke는 TYBot credential 사본을 사용하며 완료 로그에 workspace가 포함된다.
Node 공유 30건과 개별 RPC 21건, Python/계약/배포 통합 pytest 100건이 통과했다.
아래 최초 리뷰는 해결 이력으로 보존한다. Linux 실제 권한·health·모델 smoke는 남아 있다.

2026-10-12. Node 공유 25건, 개별 RPC 21건, 배치 계약 25건과
Python/Node 계약/배포 연동 pytest 95건 통과. Ruff와 diff 검사 통과.
개별 worker와 공유 worker의 소켓/서명/v3 요청은 일치한다.
서버 적용 전 아래 항목을 보완하고 재검증한다.

## Claude 보완 요청

### P1: timeout 뒤 남은 모델 호출과 동시성 슬롯

`src/rpc/answer.js`의 Promise.race는 timeout이면 모델 세션 loop가 끝나기 전에
결과를 반환한다. own.abort()는 다음 도구/회차를 막지만 현재 모델 호출에는 전달되지
않는다. `src/rpc/server.js`는 answer 반환 즉시 finally에서 release()한다.
이 조합이면 timeout된 모델 호출이 살아 있는데 새로운 요청이 슬롯을 얻어 실제
모델 호출 수가 공유 worker 상한을 초과할 수 있다. 새 공유 시험은 이 경로를 재지 않는다.

Codex가 지연 모델 세션을 주입해 실제 answerer와 concurrency gate로 재현한 결과:

```json
{"firstStatus":"failed","gateOccupied":1,"modelCallsStillActive":2}
```

상한 1에서 첫 요청이 timeout 후 슬롯을 반환하고 두 번째 요청을 수락했지만,
첫 모델 세션이 아직 끝나지 않아 살아 있는 작업이 2개다. 실제 API 키 없이 재현했다.

모델 SDK 호출까지 AbortSignal을 전달해 timeout/취소 시 진행 중 호출을 중단하고,
실행 loop가 종료되기 전에는 실제 작업 슬롯을 반환하지 않도록 정리한다. 도구 HTTP도
진행 중인 연결 취소를 검토한다. 지연된 모델 가짜로 timeout 이후 두 번째 요청이
들어와도 살아 있는 작업 수가 상한을 넘지 않는 시험을 추가한다. 다른 요청 취소 격리와
개별 worker 회귀시험도 유지한다.

### P2: health smoke의 HMAC 읽기 경로

handback §10의 `sudo -u tybot` 명령은 `/etc/hermes-rpc-shared/hmac`을 읽는다.
그 디렉터리/파일은 root:hermes-rpc-shared 0750/0640이므로 TYBot은 읽을 수 없다.
TYBot 사본 `/etc/tybot/credentials/specialist-v3/hermes-shared.hmac`을 사용한다.
줄바꿈은 `.rstrip()` 대신 `CredentialStore.get('hermes', 'shared')`로 읽어 기존
HMAC 정규화와 일치시킨다. worker credential 접근권한을 TYBot에 추가해서 해결하지 않는다.

### P2: 공유 완료 로그의 workspace 누락

현재 server.js의 정상 완료 한 줄에는 request ID와 계측값만 있고 workspace가 없다.
공유 service에는 여러 workspace의 요청이 섞여 들어오므로 완료 로그에 검증된
`request.workspace`를 추가한다. 원문/질문/검색어/키/token은 계속 제외한다.
회신의 "workspace key를 기록한다" 설명과 실제 완료 로그가 일치하는 시험을 추가한다.

## Codex 반영

- shared 목록 부분집합 검증을 `_start_external_v3`의 early return 앞에서 수행한다.
  Master 전환이 OFF여도 잘못된 shared 목록은 기동을 막는다.
- 중앙 `deploy/install.sh`가 공유 unit helper를 호출하도록 연결한다. 자동 enable/start는 없다.
- workspace별 준비 상태와 전체 health를 구분한다. 기존 Python health는 추가 workspaces
  필드를 버리므로 운영 smoke에서는 원시 응답의 workspaces를 확인한다.

## 운영 smoke - TYBot credential 사용

```bash
sudo -u tybot /opt/tybot/.venv/bin/python - <<'PY'
import json
from tybot.specialist_http import UnixSocketTransport
from tybot.specialist_v3_secrets import CredentialStore
from tybot import specialist_wire as signing
_, secret = CredentialStore('/etc/tybot/credentials/specialist-v3').get('hermes', 'shared')
body = b'{}'
transport = UnixSocketTransport('/run/tybot-subbots/hermes-shared/http.sock')
reply = transport.post(
    signing.HEALTH_PATH, body,
    signing.signature_headers(secret, method='POST', path=signing.HEALTH_PATH, body=body),
    timeout_s=2,
)
assert reply.status == 200, f'health HTTP {reply.status}'
data = json.loads(reply.body)
signing.validate_health(reply.body)
assert data.get('workspaces', {}).get('tyit') is True, 'TYIT profile not ready'
print(data)
PY
```

기존 health는 요청의 HMAC을 검증한다. health 응답 자체에는 answer용 서명이
붙지 않으므로 "서명된 요청의 왕복"으로 검증한다. 모델 동작과 실제 workspace ACL은
Linux 서버의 새 질문으로 별도 확인한다.
