# 전문 봇 2단계 — 검증 인계서

> 작성: 2026-09-10. 구현 1~7단계 완료 후.
> 설계: [`../design/specialist-runtime-v2.md`](../design/specialist-runtime-v2.md)
>
> **이 문서를 읽는 사람에게.** 여기 적힌 것은 "무엇을 확인하라" 가 아니라
> **"무엇이 아직 확인되지 않았는가"** 다. 개발 PC 에서 돌 수 있는 것은 이미
> 테스트로 고정했다(아래 §이미 고정된 것). 남은 것은 그 테스트가 원리상 닿을
> 수 없는 자리들이고, 그 목록이 §서버에서만 확인되는 것 이다.

## 왜 이 경계가 생겼나

개발 PC 는 Windows 다. 그래서 **원리상 확인할 수 없는 것이 셋** 있다.

| 없는 것 | 그래서 확인 못 하는 것 |
|---|---|
| `socket.AF_UNIX` | 실제 Unix socket 연결·권한·경합 |
| Podman·systemd | 컨테이너 격리, cgroup 상한, unit 기동 |
| egress 강제 수단 | `network_profile` 이 실제로 막는가 |

이 셋을 흉내 내는 대신 **전송과 계약을 갈라 놨다.** 계약(무엇을 보내고 무엇을
받아들이는가)은 순수 함수라 전부 고정돼 있고, 전송은 프로토콜 하나 뒤에 있다.
그래서 서버 검증이 볼 것은 "계약이 맞나" 가 아니라 **"격리가 실제로 되나"** 다.

## 구현된 것과 그 자리

| 단계 | 산출물 | 테스트 |
|---|---|---|
| 1 계약·DB | `deploy/sql/specialist_runtime_schema.sql`, `src/tybot/specialist_wire.py` | `tests/test_specialist_wire.py` (34) |
| 2 HTTP 어댑터 | `src/tybot/specialist_http.py` | `tests/test_specialist_http.py` (28) |
| 3 런타임 템플릿 | `deploy/tybot-specialist@.service`, `deploy/tybot-specialist-run`, `deploy/tybot-specialists.target` | `tests/test_specialist_runtime_unit.py` (38) |
| 4 소스 검역 | `src/tybot/console/specialist_source.py` | `tests/test_specialist_source.py` (70) |
| 5 격리 builder | `deploy/tybot-specialist-build` | `tests/test_specialist_runtime_store.py` |
| 6 승인·배포 | `deploy/tybot-specialist-deploy`, `src/tybot/console/specialist_runtime_store.py` | 같은 파일 |
| 7 콘솔 API | `src/tybot/console/app.py` `/api/specialist-runtime/*` | `tests/test_console_api.py` |

8단계(Hermes pilot)는 시작하지 않았다. 운영 활성화 전이다.

## 이미 고정된 것 — 다시 하지 않아도 된다

되돌림 실험으로 **각 성질이 실제로 잡히는 것까지** 확인했다. 즉 "테스트가 있다"
가 아니라 "그 테스트가 그 버그를 잡는다" 를 확인했다.

- 권한 범위가 다른 근거를 한 요청에 섞으면 거부
- 응답의 자체 출처·Slack URL·`file://`·script 거부
- 우리가 주지 않은 evidence ID 인용 거부
- 승인 버전과 다른 응답 거부(응답값으로 DB 를 갱신하지 않는다)
- 제어문자를 **먼저** 걷어낸 뒤 금지어 검사(순서를 뒤집으면 제로폭 문자로 우회)
- timeout·4xx/5xx·JSON 오류·서명 불일치에서 마스터 폴백, **재시도 없음**
- circuit: 연속 실패 3회 / 최근 5분 위반률 20%(표본 5건 이상) / 성공 하나로
  닫히지 않음 / 60초 뒤 재시도
- ZIP: 경로 탈출·심볼릭 링크·실행 비트·압축 폭탄·중첩 아카이브·`.env`·키 파일·
  `node_modules`·lockfile 없음·npm 외 패키지 매니저 거부
- Git: 움직이는 ref(`main`/`latest`/`HEAD`)·비HTTPS·비GitHub·URL 자격·
  submodule·LFS·중첩 저장소 거부
- 매니페스트: 알 수 없는 runtime·release_type·network_profile 거부,
  entrypoint 는 `node` 로 시작하고 셸 문자 금지
- helper: 동작 3개·인자 2개만, key 형식, digest 형식, **그 전문가의** digest
- 상태 전이: 실패한 빌드 → 준비됨 불가, retired → active 불가
- 요청자 ≠ 승인자(대소문자 우회 포함)

## 서버에서만 확인되는 것

여기부터가 **CODEX 가 할 일**이다. Rocky staging 에서 확인한다.

### A. 인프라 게이트 (이것이 안 되면 나머지는 의미 없다)

설계 §구현 전 인프라 게이트. **셋 다 확인되기 전에는 `enabled` 로 넘기지 않는다.**

1. **Podman ↔ systemd 연동** — `tybot-specialist@hermes.service` 가 `Type=notify`
   로 뜨는가. `--sdnotify=conmon` 이 실제로 준비 신호를 보내는가.
   안 되면 `Type=exec` 로 내려야 하고, 그러면 smoke 타이밍이 달라진다.
2. **컨테이너별 egress allowlist 강제 수단** — 방화벽/프록시가 컨테이너 단위로
   목적지를 막을 수 있는가. **없으면 `network_profile=anthropic-only` 를 켜지
   않는다.** helper 는 `TYBOT_EGRESS_NETWORK` 가 없으면 기동을 거부하도록
   이미 만들어져 있다. 그 거부가 실제로 걸리는지 확인.
3. **용량·보존 정책** — `/var/lib/tybot/subbot-quarantine`, 이미지 저장소,
   빌드 로그. 검역 청소는 7일(`specialist_source.QUARANTINE_TTL`)이지만
   **타이머가 아직 없다** — 아래 §알려진 빈틈 참조.

### B. 격리가 실제로 되는가

컨테이너 안에서 시도해 **전부 실패해야** 한다.

```bash
# 전문 봇 컨테이너 안에서
cat /etc/tybot/tybot.env            # 실패해야 한다
ls /var/lib/tybot/archive           # 실패해야 한다
env | grep -i DATABASE_URL          # 아무것도 안 나와야 한다
curl -s https://example.com         # network_profile=none 이면 실패
ls /run/podman/podman.sock          # 없어야 한다
```

빌드 컨테이너 안에서도 같은 것을 확인한다. 빌드는 임의 코드 실행이라 이쪽이
더 중요하다.

상한도 실측한다 — CPU 1, 메모리 512MB, PID 128. `--memory` 는 선언만으로는
안 걸리는 경우가 있다(cgroup v2 여부).

### C. Unix socket 경로

개발 PC 에서 한 줄도 못 돈 자리다.

- 소켓 권한: `specialist-hermes` 사용자만 접근 가능한가. `tybot` 이 연결할 수
  있는가(양쪽 다 필요하다 — 마스터가 부르고 전문가가 듣는다)
- 컨테이너를 kill 했을 때 소켓이 남는가. 남으면 다음 연결이 걸린다
  (`ExecStopPost` 가 지우게 해 뒀다 — 실제로 지워지는지)
- 응답이 64KB 를 넘을 때 우리가 끊는가(`_read_all` 의 상한)
- 15초 deadline 이 실제로 걸리는가

### D. HMAC 왕복

`specialist_wire` 의 서명 함수는 고정돼 있지만, **상대 구현과 맞는지는 확인
안 됐다.** 전문 봇 쪽이 같은 정규화를 쓰는지 첫 왕복에서 본다.

- 서명 대상: `METHOD\npath\ntimestamp\nnonce\nsha256(body)`
- 응답 서명: `request_id\nsha256(body)`
- 시계 오차 30초, nonce 재사용 금지 — **nonce 저장은 전문 봇 쪽 책임**이다.
  우리는 보내기만 한다. 그쪽이 실제로 거부하는지 확인해야 한다

### E. 수명주기 왕복

```
제출 → 빌드 → 승인 → standby → smoke → active → disable → rollback
```

- 후보 smoke 실패 시 **기존 active 가 계속 답하는가**
- disable 직후 새 질문이 마스터로 가는가(DB 가 먼저 닫는다)
- 롤백 뒤 이전 digest 로 도는가
- TYBot 재시작 뒤 DB active digest 와 unit 상태가 맞는가
  (`tybot-specialist-deploy reconcile`)
- 전문 봇 프로세스 kill / 소켓 제거 / 깨진 응답에서 **Slack 답변이 유지되는가**

### F. 콘솔 화면

- 개발자/관리자/게스트가 각각 무엇을 보고 무엇을 누를 수 있는가
- 요청자 = 승인자일 때 활성화가 막히는가
- 검역 ZIP 을 승인 전에 다운로드할 수 없는가(설계상 파일명·해시·검사 결과만)

## 알려진 빈틈 — 구현하지 않았거나 반쪽인 것

**숨기지 않는다.** 여기 적힌 것을 모르면 검증이 "통과" 로 끝나고 운영에서 터진다.

| 빈틈 | 지금 상태 | 필요한 것 |
|---|---|---|
| 검역 청소 타이머 | `specialist_source.sweep()` 함수만 있고 **타이머 unit 이 없다** | `tybot-specialist-sweep.timer` 추가 |
| 빌드 실행 경로 | 콘솔은 DB 에 제출만 남긴다. `tybot-specialist-build` 를 **부르는 것이 없다** | 배포 helper 와 같은 방식의 요청 파일/타이머 |
| 승인 요청 흐름 | `specialist_change_request` 재사용을 전제했으나 **buildId 를 담는 경로가 미구현** | API 한 개 추가 |
| 라우터 연결 | `execution_mode=http` 인 전문가를 `specialist_router` 가 아직 **HTTP 로 부르지 않는다**. 지금은 프롬프트 어댑터만 탄다 | `specialist_adapters.build()` 에 http 갈래 추가 |
| health 주기 검사 | 1분 주기 검사기가 **없다**. `specialist_http.health()` 함수만 있다 | 타이머 또는 봇 내부 스케줄 |
| nonce 재사용 방지 | 우리는 보내기만 한다. 거부는 **전문 봇 쪽 책임** | 템플릿에 명시 필요 |
| SBOM | `syft` 가 없으면 lockfile 해시로 대체한다 — 완전한 SBOM 이 아니다 | syft 설치 또는 대체 수용 결정 |
| Podman `--squash-all` | 옵션 지원 여부를 서버에서 확인 안 함 | 미지원이면 빼야 한다 |

> **2026-09-11 갱신.** Hermes 는 이 경로(HTTP 실행형)로 가지 않기로 했다.
> 소스를 읽어 보니 값이 **도구 호출 루프**에 있었고(검색→읽기→재검색), 그것은
> 우리 아카이브 위에 우리 권한으로 다시 만드는 편이 낫다 —
> `src/tybot/specialist_tools.py` · `ToolSpecialist`. 이 문서의 검증 대상은
> **자기 코드를 들고 오는 다른 팀 봇**(법률·건설·회계)이다.

**가장 큰 것은 「라우터 연결」이다.** 지금 상태로 배포하면 실행형 전문가를
등록·빌드·배포할 수 있지만 **질문이 그쪽으로 가지 않는다.** 1~7단계는 그
경로를 안전하게 만드는 일이었고, 연결은 8단계(pilot)에서 한 워크스페이스에만
켜는 것이 설계 의도다. 그 전에 위 검증이 끝나야 한다.

## 돌리는 법

```bash
# 개발 PC 에서 되는 것 전부
cd /opt/tybot
.venv/bin/python -m pytest tests/test_specialist_wire.py \
                          tests/test_specialist_http.py \
                          tests/test_specialist_source.py \
                          tests/test_specialist_runtime_unit.py \
                          tests/test_specialist_runtime_store.py -q
.venv/bin/python -m ruff check src tests scripts

# 스키마 적용 (아직 안 됐다)
sudo cat /opt/tybot/deploy/sql/specialist_runtime_schema.sql \
  | sudo -u postgres psql -p 55432 -d tyslackai -f -
```

`psql` 포트는 **55432** 다. 기본 5432 로 붙으면 소켓을 찾다가 실패한다.
그리고 `/opt/tybot` 아래 파일은 `postgres` 사용자가 못 읽으므로
`sudo cat <파일> | sudo -u postgres psql -p 55432 -d tyslackai -f -` 형태로
넘겨야 한다.

## 검증 결과를 어디에 남기나

`docs/verification/` 아래 날짜를 붙인 파일 하나. 통과한 것보다 **막힌 것과 그
이유**를 적는 것이 중요하다 — 통과는 다음 사람이 다시 확인할 수 있지만, 막힌
이유는 그때 그 자리에 있던 사람만 안다.
