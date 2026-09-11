# 전문 봇 검증 인계서

> 갱신: 2026-09-11. **방향이 바뀌었다** — 이전 판(09-10)은 Hermes 를 격리
> 컨테이너로 돌리는 것을 전제했다. 그 팀 소스를 읽고 바꿨다.
>
> 설계: [`../design/specialist-runtime-v2.md`](../design/specialist-runtime-v2.md) ·
> [`../design/specialist-deployment.md`](../design/specialist-deployment.md)
>
> **이 문서가 말하는 것은 "무엇을 확인하라" 가 아니라 "무엇이 아직 확인되지
> 않았는가" 다.** 개발 PC 에서 돌 수 있는 것은 이미 테스트로 고정했고, 되돌림
> 실험으로 **그 테스트가 그 버그를 잡는 것까지** 확인했다. 남은 것은 그 테스트가
> 원리상 닿을 수 없는 자리들이다.

## 지금 무엇이 도는가

전문 봇은 **두 갈래**다. 섞으면 검증이 엉킨다.

| | Hermes (지금) | 다른 팀 봇 (앞으로) |
|---|---|---|
| `execution_mode` | `tools` | `http` |
| 어디서 도나 | 마스터 프로세스 안 | 격리 Podman 컨테이너 |
| 아카이브 접근 | 우리 도구로, 우리 ACL | 못 한다. 근거를 받는다 |
| 우리가 받는 것 | 계약(프롬프트·규칙) | 소스 → 빌드 → 이미지 |
| 검증 초점 | **권한·도구·출처** | **격리·빌드·배포** |

Hermes 가 `tools` 인 이유: 그 팀의 값이 모델이 아니라 **찾는 방식과 판정 규칙**에
있다(`ref/hermes` 20746a9 — 모델이 `search`→`read_channel`→재검색을 스스로 돈다).
규칙은 글이라 옮길 수 있고, 옮기면 근거가 밖으로 안 나가고 그 팀 배포를 기다리지
않는다.

## 원칙이 바뀌었다 (2026-09-11, 오너 결정)

**「아카이브된 것만」 → 「아카이브 + 실시간」.** 사람이 봇과 이어서 묻기 때문이다 —
방금 오간 이야기가 검색에 안 잡히면 "모른다" 만 답하게 되고, 그건 대화가 아니다.

`CLAUDE.md` 와 `AGENTS.md` **양쪽**에 적었다(Codex 는 뒤를 읽는다). 넓힌 것은
**근거의 범위뿐**이고 나머지는 그대로다. 리뷰할 때 이 넷을 본다.

1. 실시간 조회도 **아카이브와 같은 권한 기준** — 요청자가 볼 수 있는 채널만
2. **봇 발언은 안 싣는다**(원칙 1). 실으면 우리 답이 다음 답의 근거가 된다
3. 출처는 **Slack 메시지 링크**, 답변에 `[실시간]` 표시(원칙 2)
4. 실시간으로 읽은 것을 **아카이브에 쓰지 않는다.** 수집은 마스터 몫이다

## 구현된 것과 그 자리

### A. Hermes 경로 (`tools`) — 켜져 있다

| 무엇 | 파일 | 테스트 |
|---|---|---|
| 도구 4개 (우리 ACL 위) | `src/tybot/specialist_tools.py` | `test_specialist_tools.py` |
| 도구 루프 | `specialist_adapters.ToolSpecialist` | `test_tool_specialist.py` |
| 게이트웨이 도구 지원 | `src/tybot/gateway/` | `test_gateway.py` |
| 계약(프롬프트) | `subbots/hermes/contract/prompt.md` | `test_specialist_adapters.py` |
| 연동 메타데이터 | `subbots/hermes/tybot-specialist.toml` | 같은 파일 |

### B. 다른 팀 봇 경로 (`http`) — 아직 안 켠다

| 단계 | 파일 |
|---|---|
| 1 계약·DB | `deploy/sql/specialist_runtime_schema.sql`, `specialist_wire.py` |
| 2 HTTP 어댑터 | `specialist_http.py` |
| 3 런타임 템플릿 | `deploy/tybot-specialist@.service`, `-run`, `.target` |
| 4 소스 검역 | `console/specialist_source.py` |
| 5 격리 builder | `deploy/tybot-specialist-build` |
| 6 승인·배포 | `deploy/tybot-specialist-deploy`, `console/specialist_runtime_store.py` |
| 7 콘솔 API | `console/app.py` `/api/specialist-runtime/*` |

8단계(pilot)는 시작 안 했다.

## 이미 고정된 것 — 다시 하지 않아도 된다

**Hermes 경로**
- 도구가 `store.search(query, ctx)` · `visible_docs(ctx)` 만 쓴다. ACL 을 다시
  구현하지 않는다
- `ctx` 가 `ToolBox` 안에 갇혀 있다 — 인자로 못 받는다(시그니처를 검사)
- 도구가 파일을 직접 안 연다(`open`·`Path`·`glob` 부재를 검사)
- 실시간 조회가 가려진 채널을 거부한다
- 봇 발언을 안 싣는다
- 루프 상한 8회, 상한 뒤에는 **도구 없이** 한 번 더 묻는다
- tool_use ↔ tool_result 짝이 맞는다(안 맞으면 다음 호출이 400)
- 비용이 회차마다 누적된다
- 출처를 **전문가가 읽은 것**으로 만든다. 실시간은 Slack 링크
- 계약 파일이 `subbots/` 와 옛 경로에 **동시에 있지 않다**
- 배포가 `subbots/*/contract/prompt.md` 배치를 검증한다

**공통 (게이트웨이)** — 같은 실수를 세 번 하고 얻은 규칙이다
- `system` 은 콘텐츠 블록 배열, 없으면 키 생략
- `temperature` 는 레지스트리가 허용한 모델에만
- 도구가 없으면 인자 자체를 안 보낸다
- 모델별 제약은 `ModelSpec` 이 들고, 프로바이더에 모델 이름이 없다

**HTTP 경로** — 권한 범위 혼합 거부, 자체 출처·URL 거부, 알 수 없는 evidence ID
거부, 버전 불일치 폐기, 제어문자 제거 순서, 재시도 없음, circuit 세 기준,
ZIP·Git 검역 전부, helper 인자 거부, 상태 전이, 요청자≠승인자.

## 서버에서만 확인되는 것

### A. Hermes 경로 — **지금 바로 할 수 있다**

개발 PC 에서 못 본 것은 **실제 Slack·실제 아카이브·실제 모델**이다.

**A-1. 도구가 실제로 불리는가**

Slack 에서 요약을 묻고 콘솔 「최근 호출」 을 본다.

- `성공` 인가(이전엔 `마스터 폴백` 이었다)
- 응답이 수 초대인가 — 도구를 도니 프롬프트형보다 느린 것이 정상이다
- 비용이 0 이 아닌가

```bash
journalctl -u tybot --since today | grep -E "도구 실패|전문가 도구 루프 상한"
```
「루프 상한」 이 자주 보이면 8회로 부족하거나 검색이 안 맞는 것이다.

**A-2. 권한이 실제로 막히는가 — 가장 중요하다**

같은 질문을 **비공개 채널 멤버**와 **비멤버**로 각각 묻는다.

- 비멤버 답에 그 채널 내용이 나오면 **즉시 되돌린다**(아래 §되돌리기)
- 출처에 그 채널이 뜨는지도 본다

구조상 새지 않아야 한다 — `ToolBox` 는 요청마다 새로 만들어지고 `ctx` 가 안에
갇혀 있다. 그래도 **실측으로 한 번은 확인해야 한다.** 우리가 못 본 경로가 있을
수 있고, 여기서 새면 되돌릴 수 없는 종류의 사고다.

**A-3. 실시간 조회**

방금 올린 메시지를 두고 "방금 뭐라고 했죠?" 라고 묻는다.

- `[실시간]` 표시가 붙는가
- 출처가 **Slack 링크**인가(아카이브 문서가 아니라)
- **그 링크를 눌러 실제 메시지로 가는가** — 우리가 링크를 조립하므로 형식이
  틀릴 수 있다(아래 §알려진 빈틈)
- 봇이 직전에 한 답을 근거로 쓰지 않는가
- 봇이 없는 비공개 채널을 지정하면 거부되는가

**A-4. 출처가 답과 맞는가**

답에 나온 숫자가 출처로 붙은 문서에 **실제로 있는지** 본다. 도구형은 마스터가
고른 것과 다른 문서를 열기 때문에, 여기가 어긋나면 사람이 확인하러 갔다가 못
찾는다 — 그 순간 출처는 신뢰를 만드는 것이 아니라 깎는다.

**A-5. 버전 표시**

콘솔 전문 봇 목록에 `version`(승인)과 `deployedVersion`(지금 파일)이 나란히 나온다.
갈려 있으면 **승인 절차 밖에서 프롬프트가 바뀐 것**이다.

### B. HTTP 경로 — 인프라 게이트가 먼저다

**셋 다 확인되기 전에는 `enabled` 로 넘기지 않는다.**

1. **Podman ↔ systemd** — `Type=notify` 와 `--sdnotify=conmon` 이 실제로 붙는가
2. **컨테이너별 egress allowlist 강제 수단** — 없으면 `anthropic-only` 를 켜지
   않는다. helper 는 `TYBOT_EGRESS_NETWORK` 가 없으면 기동을 거부한다. 그 거부가
   실제로 걸리는지 확인
3. **용량·보존 정책** — 검역·이미지 저장소·빌드 로그

그다음 격리 실측(컨테이너 안에서 `/etc/tybot`·아카이브·DB·`podman.sock` 접근이
전부 실패해야 한다), Unix socket 권한·수명, HMAC 왕복, 수명주기 왕복(후보 smoke
실패 시 기존 active 유지 / disable 직후 마스터 폴백 / 롤백 / 재시작 reconcile),
화면 권한(개발자·관리자·게스트).

## 알려진 빈틈 — 숨기지 않는다

| 빈틈 | 지금 상태 | 어느 경로 |
|---|---|---|
| **도구 호출 기록 없음** | `specialist_call` 에 도구를 몇 번 불렀는지 안 남는다 | Hermes |
| **실시간 링크 형식** | `permalink` API 를 안 부르고 우리가 조립한다 | Hermes |
| 질문 하나의 금액 상한 | 회차 8회로만 묶여 있다. 일일 상한에는 잡힌다 | Hermes |
| health 주기 검사 | 1분 검사기가 없다(함수만 있다) | HTTP |
| 검역 청소 타이머 | `sweep()` 함수만 있고 타이머 unit 이 없다 | HTTP |
| 빌드 실행 경로 | 콘솔은 DB 에 제출만 남긴다. builder 를 부르는 것이 없다 | HTTP |
| 승인 요청에 buildId | 미구현 | HTTP |
| nonce 재사용 방지 | 우리는 보내기만 한다. 거부는 전문 봇 책임 | HTTP |
| SBOM | syft 없으면 lockfile 해시로 대체 | HTTP |

**Hermes 경로에서 가장 신경 쓸 것은 「도구 호출 기록」이다.** 지금은 답이 느려도
검색을 몇 번 돌았는지 알 수 없어, 프롬프트를 고칠지 검색을 고칠지 판단할 근거가
없다.

## 검토 DM (별건 — 이것이 Hermes 와 우리의 차이다)

`/채널` 검토자가 요약본을 DM 으로 받아 **검증**하고, 맞는 것만 반영한다.
2026-09-11 까지 **한 건도 안 나갔다.** 원인 둘이 같은 증상으로 겹쳐 있었다.

1. `review_digest_sent` 에 봇 역할 GRANT 누락 → 첫 조회가 `permission denied`
2. `tybot-review-dm.timer` 가 enable 조차 안 돼 있었음

둘 다 "보낼 것이 없다" 와 구별되지 않았다. 지금은 `schema_problem()` 이 쓰기까지
시험하고, `/채널 상태` 에 「검토 DM」 항목이 생겼다.

**검증**: 검토자를 정한 채널에서 `/채널 상태` → 「검토 DM」 이 🟢 인가.
그리고 다음 날 실제로 DM 이 오는가.

## 돌리는 법

```bash
cd /opt/tybot
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests scripts
```

스키마 — **두 파일 다** 적용한다:

```bash
sudo cat /opt/tybot/deploy/sql/specialist_runtime_schema.sql \
  | sudo -u postgres psql -p 55432 -d tyslackai -f -
sudo cat /opt/tybot/deploy/sql/review_digest_schema.sql \
  | sudo -u postgres psql -p 55432 -d tyslackai -f -
```

포트는 **55432** 다. `/opt/tybot` 아래 파일은 `postgres` 사용자가 못 읽으므로
`sudo cat <파일> | sudo -u postgres psql -p 55432 -d tyslackai -f -` 형태로
넘겨야 한다.

### 되돌리기

도구형이 이상하면 **배포 없이 즉시**:

```sql
UPDATE specialist_bot SET execution_mode = 'prompt' WHERE key = 'hermes';
```

라우팅 자체를 끄려면 `state = 'disabled'`. 되돌리기가 배포를 타지 않는 것이
중요하다 — 이상하게 답할 때 기다릴 수 있는 시간은 몇 분이 아니라 몇 초다.

## 검증 결과를 어디에 남기나

`docs/verification/` 아래 날짜를 붙인 파일 하나. 통과한 것보다 **막힌 것과 그
이유**를 적는 것이 중요하다 — 통과는 다음 사람이 다시 확인할 수 있지만, 막힌
이유는 그때 그 자리에 있던 사람만 안다.
