# 공유 Hermes RPC worker — Codex 회신

날짜: 2026-10-12
인계: [`2026-10-12-hermes-shared-rpc-handoff.md`](2026-10-12-hermes-shared-rpc-handoff.md)
계약: [`specialist-v3-tool-contract.md`](../design/specialist-v3-tool-contract.md) — **스키마는 v3 그대로**

담당 경계대로 `subbots/hermes/**` 와 Hermes 전용 배치·시험·이 문서만 고쳤다.
`src/tybot/**` 와 `deploy/install.sh` 는 **건드리지 않았다.**

## 1. 전선 위는 하나도 안 바뀌었다

스키마·서명·nonce·health 계약·취소·요청 격리가 개별 worker 와 **같다.** 요청의
`workspace` 는 실제 workspace key 이고 `shared` 로 바꾸지 않는다 — `shared` 를 보내면
400 이다(명부에 없으므로).

달라진 것은 **누가 어떤 설정으로 답하는가**뿐이다. 개별 worker 는 env 하나로
domain·모델·키가 정해지고, 공유 worker 는 요청의 workspace 로 명부를 찾아 정한다.

## 2. 변경 파일

| 파일 | 무엇 |
|---|---|
| `src/rpc/profiles.js` (신규) | workspace 명부. 비밀 인라인·wildcard·모르는 필드·전역 fallback 거절 |
| `src/rpc/limits.js` (신규) | 두 겹 동시성 게이트. 대기열 없음 |
| `src/rpc/config.js` | `loadSharedRpcConfig` · `SHARED_SOCKET_PATH` 추가 |
| `src/rpc/server.js` | `route`(workspace→답변기) · `limits`(429) · `readiness`(health) |
| `src/rpc/wire.js` | `buildHealthResponse` 에 선택적 `workspaces` |
| `scripts/run-rpc-shared.js` (신규) | 공유 entrypoint |
| `scripts/check-rpc-shared.js` (신규) | 공유 계약 시험 30건 |
| `scripts/check-rpc-isolation.js` | **두 entrypoint** 를 같이 건다 |
| `scripts/check-rpc-unit.js` | 공유 배치 계약 10건 추가 |
| `scripts/check-catalog.js` | 등록 1줄 |
| `deploy/hermes-rpc-shared.service` (신규) | 공유 unit. 템플릿 아님 |
| `deploy/install-rpc-shared.sh` (신규) | 공유 설치 helper |
| `deploy/README.md` | 「공유 RPC worker」 절 |
| `package.json` | `rpc:shared` |
| `tests/test_hermes_rpc_contract.py` | 공유 검사 등록 + 계약 시험 2건 |

**개별 worker 는 한 줄도 안 바뀌었다** — `hermes-rpc@.service` · `run-rpc-worker.js` ·
`install-rpc.sh` 그대로다. 시험이 그걸 본다(`개별 unit 은 그대로다`).

### `server.js` 를 어떻게 넓혔나

단일 모드를 **라우터 하나짜리 공유 모드**로 접었다.

```js
const resolveAnswerer = routeTo || ((ws) => (ws === workspace ? answerer : null));
```

두 경로를 따로 두면 서명·재생·격리 규칙이 한쪽에만 고쳐지는 날이 오고, 그 차이는
에러 없이 흐른다. 개별 worker 의 호출 모양(`{workspace, answerer}`)은 그대로다.

## 3. 실행 명령

```bash
npm run rpc:shared                      # = node scripts/run-rpc-shared.js
systemctl start hermes-rpc-shared       # 운영
sudo bash subbots/hermes/deploy/install-rpc-shared.sh   # unit 설치 + 전제 점검
```

중앙 `deploy/install.sh` 에서 부를 자리:

```bash
bash "$REPO/subbots/hermes/deploy/install-rpc-shared.sh"
```

인자 없음. 종료코드는 모자란 것이 있어도 **0** — 설치는 됐고 준비가 덜 된 것이라
배포를 멈추지 않는다. enable·start·계정·그룹·HMAC·API 키·명부를 **만들지 않는다.**

## 4. 실제 설정 (비밀 없음)

### `/etc/hermes-rpc-shared/rpc.env`

```text
HERMES_RPC_MAX_CONCURRENT=4
HERMES_RPC_MAX_PER_WORKSPACE=2
HERMES_RPC_VERSION=2026-10-12-a1b2c3d
```

`HERMES_MODE` · `HERMES_RPC_HMAC_FILE` · `HERMES_RPC_PROFILES_FILE` 은 **unit 이
고정한다.** env 오타로 모드가 바뀌면 원문 쓰기와 독립 Slack 역할이 열린 채로 돈다.

### `/etc/hermes-rpc-shared/profiles.json` (0640 root:hermes-rpc-shared)

```json
{
  "tyit": {
    "domain": "pf-construction",
    "api_key_file": "/etc/hermes-rpc-shared/keys/tyit"
  },
  "mgmt": {
    "domain": "enterprise",
    "api_key_file": "/etc/hermes-rpc-shared/keys/mgmt",
    "model": "claude-opus-5-5",
    "effort": "high"
  }
}
```

| 필드 | 필수 | 기본 |
|---|---|---|
| `domain` | **예** | 없음 — 없으면 그 workspace 가 떨어진다 |
| `api_key_file` | **예** (절대경로) | 없음 |
| `model` | 아니오 | `claude-opus-5-5` |
| `effort` | 아니오 | `high` |
| `max_tokens` | 아니오 | `8000` |
| `max_iterations` | 아니오 | `12` |

그 밖의 필드는 **거절한다** — 모르는 필드를 조용히 무시하면 오타가 설정처럼 보인다.
`api_key`·`secret`·`token` 같은 이름이 보이면 값을 보지 않고 막는다.

같은 키 파일을 여러 profile 이 가리켜도 된다. **명시해야** 할 뿐이다.

### unit 설치

```bash
sudo install -o root -g root -m 0644 \
  subbots/hermes/deploy/hermes-rpc-shared.service \
  /etc/systemd/system/hermes-rpc-shared.service
sudo systemctl daemon-reload
sudo systemctl enable --now hermes-rpc-shared     # 사람이 한다
```

helper 가 같은 일을 멱등으로 한다(내용이 같으면 복사도 reload 도 건너뛴다).

## 5. 계약 대응 여덟

| 인계 | 어떻게 |
|---|---|
| ① 스키마 v3 그대로, `workspace` 는 실제 key | `wire.js` 무변경. `shared` 는 명부에 없어 400 |
| ② 공유 받기 소켓 고정 | `SHARED_SOCKET_PATH` 상수. **env 로 안 받는다** — 시험이 본다 |
| ③ TYBot 선택 설정 | TYBot 쪽(Codex). worker 는 공유 소켓에 오는 것만 받는다 |
| ④ HMAC `hermes-shared.hmac`, identity 하나 | worker 는 `/etc/hermes-rpc-shared/hmac` 하나. 세션 권한은 **요청의 token/ToolBox** 가 정하고 identity 가 아니다 |
| ⑤ 공유 계정·service·경로, LoadCredential 없이 | 그대로. 소켓 그룹 보정도 개별과 같은 `alignSocketDirectoryGroup` |
| ⑥ allowlist 는 서버 설정 | `profiles.json`. 요청이 model·domain·키·경로를 못 고른다 |
| ⑦ health 계약 유지 + workspace 별 구분 | `status`·`contract_version`·`version` 그대로 + 선택 `workspaces` |
| ⑧ 취소·중복·교차 금지 | 요청마다 AbortController·token·발급 집합. 시험이 동시 실행으로 잰다 |

### ④ 를 조금 더

도구 서버 쪽 credential identity 가 `hermes:shared` 하나여도 **권한이 하나로 합쳐지지
않는다.** 자료는 요청의 `tools.token` 으로만 나오고, 그 토큰은 TYBot 이 그 요청의
`ToolBox`/ACL 에 묶어 발급한다. worker 는 토큰을 **요청 세션 안에 닫아** 들고 있고,
다른 요청에 넘기는 경로가 없다 — 시험이 두 요청을 동시에 돌려 토큰이 안 섞이는지 본다.

### 요청끼리 공유하는 것 / 안 하는 것

**요청마다 새로 만든다**: 모델 client(API 키 포함) · 도구 client · 도구 객체 ·
발급 근거 집합 · trace · AbortController · 대화.

**프로세스에 하나**: HMAC 인증기(재생 방지 기억) · 동시성 게이트. 둘 다 workspace 를
가로지르는 값을 안 들고 있다.

> **모델 client 를 workspace 별로 캐시하지 않았다.** 캐시해도 기능은 같지만, 캐시가
> 있으면 「저 client 에 어느 키가 들어 있나」 를 사람이 추적해야 하고 — 그 추적이
> 틀리는 날 남의 키로 나간다. 요청마다 만들면 추적할 것이 없다. 비용은 객체 하나이고
> 동시 상한이 한 자릿수라 문제가 되지 않는다. `process.env` 는 건드리지 않는다.

## 6. 동시성 — 대기열을 만들지 않는 이유

자리가 없으면 **그 자리에서 429**(`{"error":"overloaded"}`, v2 `ERROR_CODES` 값이라
저쪽 `parse_error` 가 그대로 읽는다).

줄을 세우면 줄 끝의 요청은 저쪽 deadline 이 먼저 끝난 뒤에 모델을 부르기 시작한다 —
아무도 안 기다리는 답을 비용을 내며 만드는 상태다. 더 나쁜 것은 **보이지 않는다**는
점이다: 저쪽 로그에는 `transport`, 우리 로그에는 정상 완료가 찍힌다.

두 겹인 이유: 전체만 두면 한 workspace 가 전부를 먹고 다른 쪽이 **자기 잘못이 아닌
이유로** 막힌다. workspace 만 두면 workspace 수가 늘 때 전체 부하가 같이 는다.

슬롯은 성공·실패·취소·예외 **어느 길로 나가도** `finally` 에서 돌려주고, `release()`
는 두 번 불려도 한 번만 먹는다 — 늘어나면 상한이 조용히 무너지고, 그건 상한이 없는
것보다 나쁘다(있다고 믿으므로 아무도 안 본다).

**429 는 `request_id` 를 점유하지 않는다.** 점유하면 저쪽이 같은 id 로 재시도할 때
429 가 409 로 바뀌어 원인이 뒤바뀐다. 시험이 그 순서를 본다.

## 6.1 timeout 뒤에도 상한을 지킨다 (2026-10-14 · 리뷰 P1 반영)

Codex 가 잡은 것이 맞았다. 첫 구현은 **예산이 끝나면 답을 내보내고 슬롯도 같이
돌려줬다.** 그런데 모델 호출은 아직 살아 있었다 — `own.abort()` 가 다음 회차와 다음
도구 호출만 막고 **이미 나간 HTTP 요청에는 닿지 않았기** 때문이다.

Codex 실측: 상한 1에서 `{"gateOccupied":1,"modelCallsStillActive":2}`.

고친 것은 둘이다.

### ① 중단 신호를 **끝까지** 내려 준다

| 어디 | 무엇이 끊기나 |
|---|---|
| `tools.js` | 다음 도구 호출(호출 직전에 본다) — 이미 있던 것 |
| `tool-client.js` → `http.request({signal})` | **진행 중인 도구 왕복** |
| `answer.js` → `provider.js` → `toolRunner(params, {signal})` | **진행 중인 모델 호출** |

SDK 는 `signal` 을 `messages.create` 와 `generateToolResponse` 에 그대로 넘긴다.
`provider.js` 변경은 선택 인자라 Slack Q&A 경로는 그대로다(그쪽은 안 넘기므로 `undefined`).

### ② 일이 **실제로 끝난 뒤에** 슬롯을 돌려준다

`answer()` 는 예산 만료에서 `settled`(아직 도는 실행 루프)를 함께 돌려주고,
서버는 그것이 풀린 뒤 `release()` 한다.

```js
const settled = result?.settled;
if (settled?.then) settled.then(release, release);
else release();
```

평소 경로에서는 `settled` 가 이미 풀려 있어 같은 tick 에 돈다 — **멀쩡한 길은
느려지지 않는다**(시험이 상한 1로 연달아 두 번 보내 확인한다).

`settled` 가 깨져도 돌려준다. 한 번이라도 안 돌려주면 그 칸은 영영 줄고, 증상은
「어느 날부터 429 가 잦다」 뿐이다.

> **뒤집힌 위험 하나.** 중단이 안 먹는 상대라면 슬롯이 오래 잡혀 429 가 는다.
> 그게 맞는 모양이다 — 그 시점에 우리는 **실제로** 그만큼의 모델 호출을 쓰고 있고,
> 429 는 그 사실을 밖에서 볼 수 있게 한다. 억지로 돌려주면 숫자만 예뻐지고 비용은
> 그대로 난다.

### 재현 시험

`check-rpc-shared.js` §5b 가 **지연된 모델 세션**을 주입해 Codex 가 잰 경로를 그대로
돈다. 그 가짜는 중단 신호를 보되 **즉시 끝나지 않는다**(`teardownMs`) — 진짜 SDK 도
소켓 정리에 시간이 걸리고, **그 틈이 바로 창**이다. 0ms 로 두면 창이 없어서
「안 넘었다」 가 공짜로 참이 된다.

| 시험 | 재는 것 |
|---|---|
| 예산이 끝나도 모델 호출이 살아 있는 동안 슬롯을 안 돌려준다 | 첫 요청 `failed` 직후 둘째가 **429**. 동시에 산 모델 호출 최대 **1** |
| 취소도 같다 | `/v3/cancel` 뒤에도 같은 불변식 |
| 평소 경로는 느려지지 않는다 | 상한 1에서 연속 두 요청이 **둘 다 answered** |
| 모델 세션에 중단 신호를 실제로 내려 준다 | 네 배선 줄을 소스로 고정 |

**시험이 진짜로 잡는지 확인했다** — `settled.then(release, release)` 를 `release()` 로
되돌리면 2건이 빨개진다.

## 7. health — 전체와 workspace 별

```json
{"status":"ok","contract_version":"v2","version":"2026-10-12-a1b2c3d",
 "workspaces":{"tyit":true,"mgmt":false}}
```

저쪽 `validate_health` 는 앞의 셋만 보므로 `workspaces` 는 **계약을 안 흔든다.**
담는 것은 **key 와 준비 여부뿐**이다 — 경로·키 파일 이름·사유를 실으면 그 값이
저쪽 로그와 콘솔로 흘러 들어간다.

한 workspace 의 키 파일이 망가져도 **전체는 `ok`** 다. 「전체 실패」 로 말하면 멀쩡한
쪽까지 라우팅이 닫히고, 「전체 ok」 하나로만 말하면 그 칸이 죽은 것을 아무도 모른다.
사유는 journal 에 `[rpc-shared] 준비 안 됨 — <ws>: …` 한 줄로 남는다.

같은 이유로 **기동도 막지 않는다** — 쓸 수 있는 workspace 가 하나라도 있으면 뜬다.
하나도 없으면 던진다(돌아도 아무것도 못 하는 상태이고, 조용히 떠 있으면 아무도 안 본다).

## 8. 로그에 남기는 것

workspace key · `request_id` · 상태 · 도구 횟수 · 토큰 수 · 시간. 끝이다.
원문·검색어·질문·API 키·HMAC·token 은 journal 에 안 남는다.

workspace key 는 **남긴다** — 400 이 났을 때 운영자가 「목록에 넣는 걸 잊었나」 를
그 자리에서 판단해야 한다.

### 완료 줄에도 workspace 를 적는다 (2026-10-14 · 리뷰 P2 반영)

첫 구현의 정상 완료 줄에는 `request_id` 와 계측값만 있었다. 공유 worker 에는 여러
workspace 요청이 **섞여 들어오므로**, 그 줄로는 「어느 쪽이 느린가·어느 쪽이 근거를
못 찾나」 를 가릴 수 없었다 — 회신에 「workspace key 를 기록한다」 고 적어 놓고
실제 줄에는 없었다.

```text
[rpc] 완료 request=<id> workspace=tyit status=answered elapsed_ms=... model=... \
      stop_reason=... in_tok=... out_tok=... tool_calls=... tools=search=2/1840
```

적는 값은 **대조를 통과한 `request.workspace`** 다. 명부에 있는 key 라는 뜻이고,
그래서 임의 문자열이 로그로 들어오지 않는다. 미등록 workspace 는 400 에서 끊기므로
완료 줄 자체가 없다(시험이 그것도 본다).

개별 worker 의 완료 줄에도 같이 적었다 — 두 줄 모양이 다르면 집계가 두 벌이 된다.

## 9. 시험 결과

```bash
cd subbots/hermes
node scripts/check-rpc-shared.js      # 30건  ← 이번 작업 (리뷰 반영 5건 포함)
node scripts/check-rpc-unit.js        # 25건 (공유 10건 포함)
node scripts/check-rpc-wire.js        # 26건
node scripts/check-rpc-server.js      # 21건
node scripts/check-rpc-isolation.js   #  6건 (두 entrypoint 를 같이 건다)
```

```bash
pytest -q tests/test_hermes_rpc_contract.py        # 13 passed
ruff check src tests scripts                        # All checks passed
```

### `check-rpc-shared.js` 가 재는 것

| 절 | 무엇 |
|---|---|
| 명부 | 정상 로드 · 비밀 인라인 4종 거절 · wildcard 거절 · 모르는 필드 거절 · 한 칸만 떨어짐 · 빈 키/상대경로 거절 · 빈 명부/없는 파일 거절 |
| workspace 선택 | domain·모델·**API 키가 갈린다** · 미등록 400 · `shared` 400 |
| 교차 인용 | **동시에 돌 때** TYIT 가 MGMT 의 `[e9]` 를 인용하면 `failed`(본문도 안 나감) · 도구가 각자 토큰으로만 나감 |
| 취소 | 하나 취소해도 다른 workspace 는 완주 · 슬롯 반환 · 모르는 id 취소는 무해 |
| 동시성 | 게이트 두 겹 + 중복 반환 방지 · workspace 429 + 다른 workspace 통과 · 전체 429 · 429 가 id 점유 안 함 · 예외 뒤 슬롯 반환 |
| **예산 만료**(§5b) | **지연된 모델**로 재현 — 예산 뒤 살아 있는 호출이 상한을 안 넘음 · 취소도 같음 · 평소 경로는 안 느려짐 · 네 배선 줄 고정 |
| **완료 로그** | 섞여 들어와도 `workspace=` 로 가림 · 질문·토큰·답변 본문 없음 · 미등록은 완료 줄 자체 없음 |
| health | 전체 ok + workspace 별 · 2초 안 · 비밀/경로 안 샘 · 개별 worker 는 `workspaces` 없음 |
| 그대로인 것 | 서명 401 · nonce 재사용 401 · 중복 `request_id` 409 · 소켓 자리 고정 |

**교차 인용 시험은 실제로 겹쳐서 돈다.** TYIT 쪽을 도구 호출 뒤에 붙잡아 두고 그
사이에 MGMT 를 끝까지 보낸다 — 순서대로 돌면 「안 섞였다」 가 공짜로 참이 된다.

## 10. 미검증 — 리눅스 서버에서 해야 할 것

개발 PC(Windows)는 `AF_UNIX` listen 이 `EACCES` 라 시험이 named pipe 로 돈다.
`listen(path)` 와 `http.request({socketPath})` 는 같은 API 라 재는 **내용**은 같지만,
아래는 **못 쟀다**.

```bash
# ① 소켓 그룹 상속 (검사가 리눅스에서는 실제 gid 로 잰다)
cd /opt/tybot/subbots/hermes && node scripts/check-rpc-unit.js

# ② 자리와 권한
sudo systemctl start hermes-rpc-shared
stat -c '%n %U:%G %a' /run/tybot-subbots/hermes-shared \
                      /run/tybot-subbots/hermes-shared/http.sock
#   디렉터리 hermes-rpc-shared:tybot-specialist-rpc 750
#   소켓     hermes-rpc-shared:tybot-specialist-rpc 660

# ③ TYBot 계정이 닿는가
sudo -u tybot test -w /run/tybot-subbots/hermes-shared/http.sock && echo "닿는다"

# ④ 서명까지 포함한 health (모델을 안 부른다)
#    **TYBot 사본을 읽는다.** worker 쪽 /etc/hermes-rpc-shared/hmac 은
#    root:hermes-rpc-shared 0750/0640 이라 tybot 이 못 읽는다 — 읽히려고 권한을
#    넓히면 공유 worker 의 격리가 그만큼 무너진다. 줄바꿈 처리도 CredentialStore 에
#    맡긴다(.rstrip() 을 손으로 쓰면 정규화 규칙이 두 벌이 된다).
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

# ⑤ 실제 workspace 하나로 진짜 질문 한 번 (TYIT 만)
#    TYBOT_HERMES_SHARED_WORKSPACES=tyit 로 두고 Slack 에서 한 번 묻는다.
```

④가 `workspaces` 까지 돌려주면 그룹·권한·서명·경로·명부가 전부 맞은 것이다.
③까지 맞고 ④가 401 이면 **HMAC 이 TYBot 쪽과 다르다** — 개별 worker 비밀을
복사하지 않았는지 먼저 본다.

> **원시 응답의 `workspaces` 를 본다.** Python `validate_health` 는 계약대로
> `status`·`contract_version`·`version` 셋만 돌려주고 나머지를 버린다. 그래서 위
> 스크립트는 `json.loads(reply.body)` 로 한 번 더 읽는다 — `validate_health` 의
> 반환값만 보면 「한 workspace 가 죽었다」 가 안 보인다.
>
> health 응답 자체에는 answer 용 서명이 안 붙는다. 그래서 이것은 「서명된 **요청**의
> 왕복」 으로 검증하는 것이고, 응답 서명을 확인하는 것이 아니다.

**그 밖에 못 쟀다**: 실제 모델 호출(키가 workspace 별로 맞는지), 실제 부하에서의
동시 상한 값, 공유 프로세스의 메모리 사용량(`MemoryMax=3G` 는 추정치다).

## 11. 넓히는 순서 — 하나씩

재시작 한 번이 **공유 대상 전체**를 끊는다. 그래서:

1. `TYBOT_HERMES_SHARED_WORKSPACES=tyit` 로 **하나만** 보낸다
2. 며칠 돌려 보고 journal 에 429·`준비 안 됨` 이 없는지 본다
3. `profiles.json` 에 다음 workspace 를 더하고 `systemctl restart`
4. TYBot 쪽 목록에 더한다

목록 밖 workspace 는 기존 `hermes-<ws>/http.sock` 을 그대로 쓴다. **공유 연결이
실패해도 개별 worker 로 재시도하지 않는다**(인계 §3) — 재시도하면 같은 질문이 두 번
처리되고, 비용과 지연이 그만큼 는다.

## 12. 남는 위험 — 가리지 않고 적는다

**① 한 프로세스가 여러 키를 든다.** 공유 실행의 전제이고 설정으로 못 줄인다.
그 프로세스가 새면 전부 샌다. 줄이는 유일한 방법은 그 workspace 를 명부에서 빼고
**개별 worker 로 돌리는 것**이고, 그 길은 그대로 열어 뒀다.

**② 장애 반경이 넓어진다.** 재시작·OOM·버그가 공유 대상 전체를 끊는다.
`MemoryMax=3G` 를 개별 worker(1.5G)보다 넉넉히 둔 이유다 — 모자라면 OOM 으로 죽고,
그 죽음은 한 workspace 가 아니라 전부다.

**③ 다른 worker 소켓에 닿을 수 있다.** `/run/tybot-subbots/` 아래 형제를
`InaccessiblePaths` 로 가리면 자기 RuntimeDirectory 까지 함께 가려지므로 못 막는다.
닿아도 HMAC 이 달라 전부 401 이고 `workspace` 도 대조한다 — 남는 것은 **로컬 DoS** 이며
자료는 안 샌다.

**④ 키 회전은 재시작이 필요하다.** API 키를 기동 때 읽는다. 요청마다 읽으면 회전이
즉시 먹지만 파일 읽기가 요청 경로에 들어가고, 읽기 실패가 **답변 도중**의 실패가 된다.
기동 때 읽으면 못 읽은 workspace 가 health 에 바로 보인다. 회전 빈도가 올라가면
다시 볼 자리다.

## 13. 제안 — Codex 쪽

인계 지시대로 제안만 남긴다. TYBot Python 은 안 건드렸다.

**`TYBOT_HERMES_SHARED_WORKSPACES` 를 Master v2 allowlist 의 부분집합으로 검증**할 때,
어긋나면 **기동을 막는** 쪽을 권한다. 경고만 남기면 「공유로 보내는 줄 알았는데 개별로
가고 있던」 상태가 오래 간다 — 둘 다 답은 나오므로 아무도 안 본다.

**공유 목록에서 workspace 를 뺄 때** 돌고 있는 요청이 있으면 그 요청은 공유 worker 가
끝까지 처리한다(토큰이 살아 있다). 목록 변경이 **다음 요청부터** 먹는다는 점을
운영 문서에 적어 두면 좋겠다.
