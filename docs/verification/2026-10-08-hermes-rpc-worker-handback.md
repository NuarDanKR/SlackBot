# Hermes RPC worker — Codex 회신

날짜: 2026-10-08
상위 작업: B-76
대상: `docs/verification/2026-10-08-hermes-external-router-claude-handoff.md`
담당 경계: `subbots/hermes/**` + Hermes 전용 배치 파일 + 이 문서와 회귀시험

TYBot 쪽 다섯 파일(`intent.py` · `master_planner.py` · `specialist_router.py` ·
`slack/pilot.py` · `answer.py` · `tests/test_master_v2_rollout.py`)은 **건드리지 않았다.**

## 1. 실행 명령과 unit

| 무엇 | 값 |
|---|---|
| 진입점 | `subbots/hermes/scripts/run-rpc-worker.js` |
| npm | `npm run rpc` (= `node scripts/run-rpc-worker.js`) |
| 서버 | `sudo -u hermes-rpc-<ws> /usr/bin/node /opt/tybot/subbots/hermes/scripts/run-rpc-worker.js` |
| unit | `subbots/hermes/deploy/hermes-rpc@.service` → `systemctl start hermes-rpc@tyit` |
| 설치 | `sudo subbots/hermes/deploy/install-rpc.sh [<ws> ...]` — 멱등. enable·start 안 함 |
| 기본 상태 | **disabled.** `[Install]` 은 있다(없으면 `systemctl enable` 자체가 실패) |

인스턴스 이름(`%i`)이 **워크스페이스 key** 다. 소켓·계정·비밀·설정이 전부 여기서 갈린다.

> **이름은 소문자·숫자·하이픈만.** `%i` 는 이스케이프된 문자열이라 `.` 이나 `/` 가
> 들어가면 `\x2e` 로 풀려 **만들어 둔 계정 이름과 안 맞는다.** 그 실패는 `217/USER` 다.

Slack 프로세스와 **같이 띄우지 않는다**(요구 10). Socket Mode 의 ping/pong 은 한
프로세스의 이벤트 루프를 쓰는데, RPC 답변은 모델을 여러 번 부르며 수십 초를 쓴다.
같은 자리에 얹으면 ping 이 늦어 Slack 이 연결을 끊고, 끊기면 **질문이 아예 안 들어온다** —
느려지는 게 아니라 조용히 멈춘다.

### 중앙 `deploy/install.sh` 에서 부를 자리

`deploy/install.sh` 는 **안 건드렸다**(Codex 몫). 부를 것은 이것 하나다.

```bash
# 인자: 준비 상태를 점검할 워크스페이스들. 없으면 unit 설치만 한다.
bash "$REPO/subbots/hermes/deploy/install-rpc.sh" tyit
```

`bash <경로>` 로 부르기를 권한다 — 실행 비트에 기대지 않는다. 비트는 git 에
`100755` 로 들어 있지만(저장소의 다른 deploy 스크립트는 `100644` 다), zip 으로 풀거나
`core.fileMode=false` 인 체크아웃에서는 사라진다. 직접 실행도 물론 된다.

| 성질 | 값 |
|---|---|
| 경로 | `subbots/hermes/deploy/install-rpc.sh` |
| 인자 | `[<workspace> ...]` — 선택. 각 워크스페이스의 준비 상태를 **점검만** 한다 |
| 권한 | root 필요(아니면 안내하고 1 로 끝난다) |
| 하는 일 | `hermes-rpc@.service` → `/etc/systemd/system/` 0644, `daemon-reload` |
| 멱등 | 내용이 같으면 복사도 reload 도 안 한다 |
| 종료코드 | 모자란 것이 있어도 **0** — 설치는 됐고 준비가 덜 된 것이라 배포를 멈추지 않는다 |

**안 하는 것**: enable · start · 계정 생성 · 그룹 생성 · HMAC 비밀 생성 · env 생성.
특히 비밀은 양쪽이 같은 값이어야 하는데 한쪽만 새로 만들면 전부 401 이 되고,
증상은 「전문가가 조용히 답을 안 함」 이라 소켓 그룹 오류와 구별되지 않는다.
모자란 것은 **고치지 않고 칠 명령을 적어 준다** — 설치가 조용히 메워 주면 그 자리에
무엇이 필요한지 아무도 모르는 채로 운영이 시작된다.

## 2. 소켓 경로

| 방향 | 경로 | 누가 listen |
|---|---|---|
| TYBot → Hermes | `/run/tybot-subbots/hermes-<ws>/http.sock` | **이 worker** |
| Hermes → TYBot | `/run/tybot/specialist-tools.sock` | `specialist_v3_tool_server.ToolServer` |

첫 대상은 `/run/tybot-subbots/hermes-tyit/http.sock` 이다.

**경로를 설정에서 받지 않는다.** `src/rpc/config.js:socketPathFor()` 가 key 로 계산하고,
그 규칙은 저쪽 `specialist_v3_client._KEY`(`^[a-z0-9][a-z0-9-]{0,62}$`)와 같다. 받아 쓰면
그 값이 곧 「아무 자리에나 listen 하는 권한」 이고, 그 자리를 TYBot 이 부를 리 없으므로
증상은 오류가 아니라 **아무 응답이 없는 것**이다.

요청 본문의 `tools.socket` 은 **대조만** 한다 — 계약값과 다르면 400 이다. 그 값으로
실제 연결하지 않는다.

권한: 디렉터리 `0750` · 그룹 `tybot-specialist-rpc`, 소켓 파일 `0660`(프로세스가 건다).

## 3. HMAC credential

```ini
HERMES_RPC_HMAC_FILE=/etc/hermes-rpc-%i/hmac
```

Rocky 8의 systemd 239에서는 worker가 위 고정 경로를 읽는다. 환경변수에는 경로만 있고,
**환경변수 값으로는 받지 않는다** — `systemctl show` 와 `/proc/<pid>/environ` 에 그대로 보인다.
32바이트 미만은 기동을 막는다. 끝 줄바꿈 하나는 떼고 나머지는 바이트 그대로 쓴다.

> **인스턴스마다 다른 비밀을 쓰세요.** 한 벌을 돌려 쓰면 워크스페이스 A 의 worker 가
> B 의 요청을 위조할 수 있습니다 — 소켓 그룹(`tybot-specialist-rpc`)이 공유되므로
> 파일 권한만으로는 안 막힙니다. 남의 비밀 **파일**은 unit 의 `InaccessiblePaths` 가
> 가립니다.

### 남는 위험 하나 (2026-10-08 정정)

첫 회신의 unit 주석에 「다른 워크스페이스 worker 의 소켓도 안 보인다」 고 적었다.
**틀렸다.** `InaccessiblePaths` 가 가리는 것은 비밀 경로뿐이다.

`/run/tybot-subbots/` 아래 형제 디렉터리를 가리려 하면 그 아래 있는 **자기
RuntimeDirectory 까지** 함께 가려지므로, 네임스페이스로는 못 막는다.

닿아도 **답은 못 받는다** — 요청마다 HMAC 서명을 보고 비밀이 인스턴스마다 다르므로
전부 401 이고, 요청 본문의 `workspace` 도 대조한다(다르면 400).

남는 것은 **로컬 DoS** 다: 같은 호스트의 다른 worker 계정이 남의 소켓에 연결을 퍼부어
처리를 늦출 수 있다. **자료가 새지는 않는다.** 더 줄이려면 워크스페이스마다 소켓
그룹을 나눠야 하는데, 그러면 TYBot 이 그룹을 전부 들고 있어야 해서 「한 계정이 여러
워크스페이스 권한을 합쳐 갖는」 쪽으로 문제가 옮겨 간다. 그래서 **그대로 둔다.**

### 그 밖의 env (`/etc/hermes-rpc-<ws>/rpc.env`)

| 이름 | 뜻 | 없으면 |
|---|---|---|
| `HERMES_DOMAIN` | `enterprise` · `pf-construction` | **기동 거부** |
| `HERMES_RPC_MODEL` | 모델 id | `claude-opus-5-5` |
| `HERMES_RPC_MAX_TOKENS` | 출력 토큰 | `8000` |
| `HERMES_RPC_EFFORT` | `low`·`high`·`xhigh` | `high` |
| `HERMES_RPC_MAX_ITERATIONS` | 도구 왕복 상한 | `12` |
| `HERMES_RPC_VERSION` | health 가 밝히는 배포 식별자 | `unversioned` |
| `ANTHROPIC_API_KEY` | 모델 키 | SDK 가 기동 때 던진다 |

`HERMES_MODE` · `HERMES_RPC_SPECIALIST` · `HERMES_RPC_WORKSPACE` 는 **unit 이 고정한다.**
env 파일 오타로 모드가 바뀌면 원문 쓰기와 독립 Slack 역할이 열린 채로 RPC 가 돈다 —
열린 것은 원문이 늘어난 뒤에야 안다. worker 는 `HERMES_MODE=tybot` 이 아니면 종료코드 2 로 거부한다.

## 4. 구현한 wire schema

전부 `specialist_wire_v3.py` 그대로다. 새로 만든 이름은 **없다.**

| | 값 |
|---|---|
| 요청 | `tybot.specialist.request.v3` → `POST /v3/answer` |
| 응답 | `tybot.specialist.response.v3` |
| 도구 호출 | `tybot.specialist.tool.v3` → `POST /v3/tools/call` |
| 도구 결과 | `tybot.specialist.tool_result.v3` |
| 취소 | `POST /v3/cancel`, 본문 `{"request_id": …}` |
| health | `POST /v1/health`, `contract_version: "v2"` |

서명·재생 방지는 `specialist_wire.canonical`/`sign` 과 **같은 규칙**이다:
`METHOD\npath\ntimestamp\nnonce\nsha256hex(body)` 에 HMAC-SHA256, 헤더
`X-TYBot-Timestamp` · `X-TYBot-Nonce` · `X-TYBot-Signature`. 응답 서명은
`sign(secret, "<request_id>\n<sha256hex(body)>")` 로 저쪽 `verify_response_signature` 가 보는 값과 같다.
시계 오차 ±30초, nonce 는 그 창 동안 기억했다가 **재사용을 401 로** 막는다.

`check-rpc-wire.js` 가 **Python 소스를 읽어** 상수를 꺼내 대조하고, 파이썬이 있으면
서명 값을 직접 비교한다. 저쪽이 바뀌면 여기서 빨개진다.

### health 응답 예시 (시크릿 없음)

```json
{"status":"ok","contract_version":"v2","version":"2026-10-08-a1b2c3d"}
```

## 5. 상태 넷을 어떻게 가르나

| status | 언제 | 비고 |
|---|---|---|
| `answered` | 본문이 있고 **발급받은** `[eN]` 을 하나 이상 인용 | `used_evidence` 는 실제로 인용한 것만 |
| `no_evidence` | 본문이 없거나, 본문은 있는데 인용이 하나도 없다 | 임의 폴백 없음 |
| `refused` | 모델이 거절(`stop_reason: refusal`) | 임의 폴백 없음 |
| `failed` | 예외·취소·deadline·**지어낸 인용**·금지 문구 | **실패로 닫는다** |

### `failed` 는 「마스터가 대신 답한다」 가 아니다 (2026-10-08 정정)

첫 회신에 「`failed` 면 마스터가 폴백한다」 고 적었다. **틀렸다.** 실제 정책은 이렇다.

| 상황 | 마스터가 하는 일 |
|---|---|
| 전문가가 **등록되지 않았거나** 그 capability 가 없다 | 자기 지식으로 직접 답할 수 있다 |
| Hermes timeout · 서명 오류 · worker 장애 · route DB 장애 | **실패로 닫는다.** 직접 답변으로 숨기지 않는다 |
| `refused` · `no_evidence` | 그대로 전한다. 임의 폴백 없음 |

즉 **한 번 우리에게 온 질문은 어느 상태로도 「마스터 직접 답변」 으로 가지 않는다.**
장애를 직접 답변으로 덮으면, 근거로 답한 것과 기억으로 답한 것이 사람에게 **같은
모양**으로 보인다 — 그게 이 계약 전체가 막으려는 일이다.

그래서 `failed` 를 돌려주는 것은 「누가 대신 답하겠지」 가 아니라 **「이 질문은 답이
안 나갔다」** 는 뜻이다. 가볍게 쓰지 않는다. `src/rpc/answer.js` 머리말도 고쳤다.

### 「읽었다」 와 「썼다」 를 가른다

도구가 돌려준 근거 ID 를 전부 담지 않는다. 모델이 본문에 `[eN]` 으로 **인용한 것만**
담는다. 전부 담으면 출처가 답변 내용과 어긋나고, 사람이 눌러 가 보면 거기 그 얘기가 없다.

### 상한은 자른 **뒤에** 인용을 거둔다

`max_output_chars` 로 먼저 자르고 그다음에 `[eN]` 을 모은다. 순서를 뒤집으면 잘려 나간
문장의 근거를 「썼다」 고 말하게 된다.

### deadline 은 **저쪽보다 1.5초 먼저** 끝낸다

저쪽은 `deadline_ms` 에 소켓을 끊고 `transport` 로 적는다. 그 시각에 딱 맞춰 끝내면
우리가 만든 답이 전선 위에서 버려지고, **버린 이유가 저쪽 로그에만** 남는다 —
「고장」 과 「시간 초과」 를 우리 쪽에서 가릴 수 없게 된다.

예산이 끝나면 `AbortController` 를 올리고 `failed` + `uncertain: ["시간 안에 끝내지
못했습니다."]` 로 돌린다. **돌던 회차를 죽이지는 못한다** — 모델 스트림과 이미 나간
도구 호출은 각자의 상한으로 끝난다(도구 `timeoutMs`, 모델 `maxIterations`). 우리가
하는 것은 다음 회차와 다음 도구 호출을 막는 것까지다.

## 6. 제안 — v3 계약에 넣었으면 하는 것 두 가지

인계 문서 지시대로 **제안만** 남긴다. TYBot 쪽 파일은 고치지 않았다.

### (가) `validate_response` 에 금지 문구 검사가 없다

v2 `specialist_wire.validate_response` 는 `출처:` · `slack.com/archives/` · `file://` ·
`<script` · `<iframe` 을 보고 폐기한다. **v3 에는 없다.**

전문 봇이 `출처: [전산팀]공지` 를 붙여 보내면 저쪽은 그대로 받고, 사람은 **권한 판정을
거치지 않은 출처**를 사실로 본다(절대 원칙 2 — 출처는 마스터만 붙인다).

지금은 worker 가 자기 출력에서 먼저 거른다(`wire.FORBIDDEN` → `failed`). 하지만 그건
**우리가 성실한 동안만** 유효한 관문이다. 다른 전문 봇이 붙거나 우리 코드가 바뀌면 사라진다.
`specialist_wire_v3.validate_response` 에 v2 와 같은 목록을 넣는 쪽이 맞다고 본다.

### (나) `used_evidence` 의 **형식** 검사가 v3 에 없다

v2 는 `_EVIDENCE_ID_RE`(`^e[0-9]{1,4}$`)로 모양을 본다. v3 는 소속(`issued_evidence`)만
본다. 소속 검사가 더 강하므로 실질 구멍은 아니지만, 모양이 틀린 ID 는 `issued` 에
있을 수 없으니 **더 이른 자리에서 더 분명한 사유로** 떨어뜨릴 수 있다. worker 쪽은
이미 본다(`buildAnswerResponse`).

## 7. Node 시험 명령과 결과

```bash
cd subbots/hermes
node scripts/check-rpc-wire.js        # 26건
node scripts/check-rpc-server.js      # 19건
node scripts/check-rpc-isolation.js   #  6건
node scripts/check-rpc-unit.js        # 14건 (리눅스에서 3건이 더 실측된다)
npm run check:offline                 # 카탈로그에 등록돼 함께 돈다
```

pytest 로도 돈다:

```bash
pytest -q tests/test_hermes_rpc_contract.py     # 6 passed
```

| 검사 | 무엇을 재나 | 결과 |
|---|---|---|
| `check-rpc-wire.js` | 스키마·엔드포인트·상한·도구·상태가 Python 원본과 같다. `canonical`/응답 서명을 **Python 이 만든 값과 직접 비교**. 요청 파싱 거절 7종. 응답 조립 규칙. 인용·금지 문구 | 26/26 |
| `check-rpc-server.js` | 실제 소켓 왕복: 정상 답변 · 근거 없음 · 지어낸 인용 · 출처 줄 · 서명 위조 · 과거/미래 시각 · nonce 재사용 · 본문 변조 · 경로 바꿔치기 · 스키마 불일치 · 다른 workspace · 모르는 경로 · request_id 반향 · 출력 상한 · health(모델 미호출, 2초) · 같은 request_id 중복 409 · 취소 · **deadline** | 19/19 |
| `check-rpc-isolation.js` | worker 가 닿는 **12개 파일** 정적 그래프에 Slack·아카이브·문서 저장소가 없다. 외부 의존이 Anthropic SDK 뿐이다. 요청 둘이 각자 토큰으로만 부른다. 한 바퀴 도는 동안 `slack.com` 요청 0건 | 6/6 |
| `check-rpc-unit.js` | 소켓 그룹(디렉터리 0750 · 소켓 0660 · TYBot 도구 소켓 GID로 명시 정렬 · 그룹 어긋나면 기동 거부) · `[Install]` · 설치 helper(0644 설치 · daemon-reload · 멱등 · 계정/비밀/env 를 안 만듦 · enable·start 안 함) | 15/15 |

`ruff check src tests scripts` → All checks passed.

### 소켓을 어디에 열고 쟀나

운영은 Unix socket 이다. 개발 PC(Windows)는 `AF_UNIX` listen 이 `EACCES` 로 막혀 있어
시험은 **named pipe** 에 연다. `server.listen(path)` 와 `http.request({socketPath})` 는
양쪽에서 같은 API 라 재는 것이 달라지지 않는다 — 바뀌는 것은 경로 모양과 파일 권한뿐이고,
권한은 unit 이 쥔다. 리눅스에서 돌리면 같은 검사가 진짜 Unix socket 으로 돈다.

### 리눅스에서만 남은 smoke (Windows 에서 **못 쟀다**)

`check-rpc-unit.js` 는 리눅스에서 별도 그룹 디렉터리를 임시로 만들어 **소켓 그룹을
디렉터리 그룹으로 명시 정렬하는지**를 실제로 잰다. Windows 에는 gid 가 없어 그 셋을 건너뛰고,
건너뛴 사실을 「못 쟀다」 로 찍는다 — 통과와 같은 모양으로 두면 「맞았다」 와
「안 쟀다」 를 가릴 수 없다.

서버에서 한 번 돌려야 하는 것:

```bash
# 1. 배치 계약 (명시 그룹 정렬을 실제 gid 로 잰다)
cd /opt/tybot/subbots/hermes && node scripts/check-rpc-unit.js

# 2. 소켓 자리와 권한
systemctl start hermes-rpc@tyit
stat -c '%n %U:%G %a' /run/tybot-subbots/hermes-tyit                       /run/tybot-subbots/hermes-tyit/http.sock
#   기대: 디렉터리 hermes-rpc-tyit:tybot-specialist-rpc 750
#         소켓   hermes-rpc-tyit:tybot-specialist-rpc 660

# 3. TYBot 계정이 실제로 닿는가
sudo -u tybot test -r /run/tybot-subbots/hermes-tyit/http.sock && echo "읽기 OK"
sudo -u tybot test -w /run/tybot-subbots/hermes-tyit/http.sock && echo "쓰기 OK"

# 4. 서명까지 포함한 진짜 왕복 — health 는 모델을 안 부른다
sudo -u tybot /opt/tybot/.venv/bin/python - <<'EOF'
from tybot.specialist_http import UnixSocketTransport, health
secret = open('/etc/tybot/credentials/specialist-v3/hermes-tyit.hmac','rb').read().rstrip(b'\n')
ok, version = health(UnixSocketTransport('/run/tybot-subbots/hermes-tyit/http.sock'), secret=secret)
print('health', ok, version)
EOF
```

4번이 `True` 면 그룹·권한·서명·경로가 전부 맞은 것이다. 3번까지만 맞고 4번이
`False` 면 비밀이 양쪽에서 다르다.

### 쓰기 경계 목록에 두 줄이 늘었다

`tests/test_hermes_write_boundary.py` 가 Hermes 의 쓰기 진입점을 목록으로 고정한다.
이번에 둘이 늘었고 설계 문서(`docs/design/hermes-write-entrypoints.md` §2.2 · §2.4)를
먼저 고친 뒤 목록을 맞췄다.

| 늘어난 것 | 어느 쪽 | 왜 |
|---|---|---|
| npm `rpc` | **읽기** | 스케줄러를 안 올린다. 모드가 `tybot` 이라 원문 쓰기가 막혀 있다 |
| `src/rpc/server.js` | **쓰기** | `mkdirSync`·`unlinkSync` — 자기 소켓 자리만. 그래도 적는다 |

소켓뿐이라고 빼 주면, 다음 사람이 그 파일에 `writeFile` 을 더해도 관문이 안 걸린다.

## 8. 배포 전 확인할 것

0. `sudo subbots/hermes/deploy/install-rpc.sh tyit` — unit 을 깔고 **모자란 것을 적어 준다**
1. `hermes-rpc-<ws>` 계정과 `tybot-specialist-rpc` 보조 그룹 — **unit 도 helper 도 안 만든다**
2. 인스턴스별 HMAC 비밀 파일 (`0640 root:hermes-rpc-<ws>`)
3. `/etc/hermes-rpc-<ws>/rpc.env` 에 `HERMES_DOMAIN` 과 `ANTHROPIC_API_KEY`
4. `tybot` 이 `/run/tybot-subbots/hermes-<ws>/http.sock` 에 닿는가 (위 smoke 2~4번)
5. TYBot 쪽 라우팅(`specialist_router`)에서 이 전문가를 켜는 것은 **Codex 몫**이다

## 9. 전제가 바뀐 곳 하나

계약 §9 는 「우리는 Hermes 를 수정하지 않는다 — PF 가 commit 하면 받아서 쓴다」 이고,
§10.4 는 Hermes 인스턴스가 **PF GCP / TY DMZ 둘**이라고 적는다.

둘 다 지금 전제가 아니다.

- Hermes 를 우리가 고친다(이번 인계 문서 「목표」)
- PF 전용 서버와 GCP 운영은 없다. 인스턴스는 **같은 호스트의 systemd 인스턴스**로 갈린다
  (2026-10-08 오너 정정 — `docs/design/hermes-instances-one-host-2026-10-08.md`)

계약 문서는 **고치지 않았다** — 그 문서는 TY·PF 합의 기록이라 담당이 따로 있다. 다만
비용 귀속(§10.4)은 「각 인스턴스가 자기 env 의 자기 키」 라는 결론이 그대로 성립한다.
이 unit 도 인스턴스마다 다른 `EnvironmentFile` 을 쓴다.
