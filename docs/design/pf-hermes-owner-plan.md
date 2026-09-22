# Hermes 서버 이관과 TYBot 통합 — 내부 오너 계획

작성: 2026-09-21  
범위: 프금팀 Hermes 소스 스냅샷과 TYBot의 `subbots/hermes`,
`src/tybot/specialist_tools.py`

## 명칭

- **Hermes**: 프금팀이 개발·운영하는 독립 Slack 봇
- **`pf-hermes`**: 같은 서버에서 다른 제품과 충돌하지 않게 사용하는 Hermes의 운영
  식별자. 공식 제품명이나 버전명이 아니다.
- **TYBot Archive Specialist**: TYBot 내부 기록 검색 전문가. 현재 내부 등록 key는
  `hermes`지만 이 문서에서는 제품명 충돌을 피하려고 key로 부르지 않는다.
- `subbots/hermes_v1.2`: 전달받은 소스를 로컬에서 구별하려고 임시로 붙인 폴더명이다.
  `1.2`를 프금팀 공식 제품명이나 release version으로 해석하지 않는다.

## 결론

두 구현은 출신과 실행 구조가 다르다.

- **Hermes**: Slack 수집, Git 아카이브, 권한, 검색, LLM, digest,
  health와 scheduler를 모두 가진 독립 Node.js 봇
- **TYBot Archive Specialist**: TYBot이 이미 권한을 검사한 중앙 원문을 TYBot gateway와 읽기 전용
  도구로 찾고 해석하는 계약형 전문가

따라서 Hermes 코드를 TYBot 안에서 실행하거나 통째로 병합하지 않는다. Hermes에서
검증된 검색 행동만 TYBot의 권한·근거 모델 위에 다시 구현한다. 이 원칙을 어기면 Slack
토큰과 ACL이 둘로 갈리고, 같은 원문을 두 수집기가 쓰며, 전문 봇이 붙인 출처와 TYBot이
검증한 출처가 달라진다.

## 1. 소스 영역별 판정

| Hermes 소스 영역 | 기능 | TYBot 판정 |
|---|---|---|
| `src/ingest/**` | Slack 원문·스레드·첨부 수집, Git 반영 | 가져오지 않음. TYBot archive가 소유 |
| `src/archive.js` | MD 파싱, 채널 권한, 대화 검색 | 코드는 가져오지 않음. 검색 행동만 참고 |
| `src/documents/**` | 문서 목록·검색·부분 읽기·접근 판정 | 검색/큰 문서 읽기 규칙만 선택 이식 |
| `src/slack/**`, `slack-live.js` | 질문 수신, Slack 실시간 읽기·표시 | 가져오지 않음. TYBot Slack/ACL이 소유 |
| `src/llm/provider.js` | Anthropic SDK, 모델·캐시 처리 | 가져오지 않음. TYBot gateway가 소유 |
| `src/llm/qa.js` | 도구 루프, usage, fallback | 가져오지 않음. TYBot specialist adapter가 소유 |
| `src/llm/tools.js` | 검색 도구 정의와 결과 진단 | 행동 규칙만 이식 |
| `src/prompts/qa.md` | 내부 기록 답변 규칙 | TYBot 계약과 충돌하지 않는 문구만 이식 |
| `src/llm/digest.js`, `digest.js` | 일일·주간 요약 생성 | 가져오지 않음. TYBot 검토/승인 요약이 소유 |
| `src/llm/summary-check.js` | 요약과 원문 대조 | 이미 B-50/B-56 경로가 소유 |
| `src/archive-health/**` | 프금팀 아카이브 상태 | 가져오지 않음. TYBot 진단/콘솔이 소유 |
| `src/convo-log/**` | Hermes 자체 대화·비용 로그 | 가져오지 않음. TYBot QA/audit가 소유 |
| `src/scheduler/**` | digest·수집 스케줄 | 가져오지 않음. TYBot systemd timers가 소유 |
| `deploy/**` | 개인 GCP/Debian 배포 | TYBot에 이식하지 않음. 프금팀 이관 대상으로 분리 |
| `scripts/check-*.js` | 전달 스냅샷의 계약·회귀 검사 | 규칙의 근거로 참고, TYBot 테스트로 다시 작성 |

## 2. 이번에 가져온 개선

### 2.1 사람 발언과 문서 결과 분리

Hermes는 대화 아카이브와 문서 아카이브를 별도 결과 구역으로 보여 준다. TYBot은
두 종류가 같은 `RawLine`에 있지만 `[첨부추출:]`, `[첨부본문:]`, `[캔버스본문:]` 표식이
있다. 검색 결과를 `문서·첨부`와 `사람 대화`로 분리한다.

이것은 꾸밈이 아니다. 문서의 8월 금액과 사람이 9월에 정정한 금액이 한 목록에 섞일 때
모델이 하나를 골라 합치는 오류를 줄이고, AGENTS 원칙 7의 시점 병기를 가능하게 한다.

### 2.2 부분 일치 정도 표시

TYBot 검색은 이미 OR 후보를 가져와 낱말 일치 수로 정렬했지만, 도구 출력은 그 사실을
숨겼다. 이제 모든 낱말이 맞지 않은 줄에 `(2/3 낱말)`처럼 표시한다. 모델이 부분 일치를
정확한 일치로 오해하지 않고 추가 읽기 여부를 결정할 수 있다.

### 2.3 채널을 먼저 좁히고 검색

기존 `where`는 전체 검색 상위 30건을 받은 뒤 문자열로 잘랐다. 다른 채널이 상위 30건을
채우면 지정 채널에 자료가 있어도 0건이 됐다. 이제 다음 순서를 지킨다.

1. 현재 요청자에게 보이는 문서만 연다.
2. 정확한 채널명 또는 유일한 부분 이름만 허용한다.
3. 모호하면 첫 채널을 고르지 않고 정확한 이름을 요구한다.
4. 좁힌 문서 집합에서 검색과 0건 진단을 수행한다.

`channels` 인자는 `ArchiveStore.visible_docs(ctx)` 결과를 더 줄이는 기능일 뿐, 권한 밖
문서를 추가할 수 없다.

### 2.4 별칭 재검색 한도

Hermes의 QA 규칙처럼 “없음” 결론 전에 표기·약칭 전환을 허용하되 한 번으로 제한한다.
후보 줄이나 파일명에서 구체적인 별칭 단서가 있을 때만 한다. 무관한 사업장 이름을
추측해 넓히는 것은 금지한다. 실제 호출 수·시간·읽기량 제한은 TYBot 코드가 계속 센다.

## 3. 의도적으로 가져오지 않은 중복 책임

| 책임 | 유일한 소유자 | TYBot Archive Specialist가 하면 안 되는 것 |
|---|---|---|
| 질문 의도·복합 작업 계획 | TYBot master | 스스로 다른 전문 봇 호출 |
| Slack 사용자·채널 권한 | TYBot access/store | 자체 private channel 정책 적용 |
| 원문 수집·변환·보존 | TYBot archive | Git/Slack에서 독자 수집·쓰기 |
| PII 판정 | TYBot 수집 관문 | 모델 판단으로 차단/허용 변경 |
| 모델 선택·키·비용 | TYBot gateway | Anthropic SDK 직접 사용 |
| timeout·fallback·circuit | TYBot specialist runtime | 자체 재시도로 총 제한 우회 |
| 최종 출처·EvidenceRef | TYBot answer/evidence | 출처 문자열·URL 생성 |
| Canvas·Slack 메시지 표시 | TYBot master/slack | 직접 Canvas/DM 작성 |
| 승인 요약 저장 | TYBot summary review | 자기 요약을 원문으로 되먹임 |

TYBot Archive Specialist는 질문에 필요한 원문을 찾고, 읽은 사실을 정확히 요약하는
데서 끝난다. 판단,
조언, 최신 외부정보, 법령 해석, 장문 보고서는 각각 마스터 또는 B-52~B-55의 다른
전문 능력이다.

## 4. 후속 이식 후보와 선행 조건

Hermes의 큰 문서 읽기는 sheet/month/week/section 목차와 잘림 안내가 정교하다.
TYBot의 `read_document`는 현재 이름이 들어간 줄만 최대 30,000자 돌려준다. 다음 단계로
가치가 있지만, 해당 코드를 그대로 복사해서는 안 된다.

선행 조건:

1. TYBot 변환 원문의 문서 경계를 file ID로 안정적으로 식별한다.
2. 제목이 같은 여러 파일과 버전을 구분한다.
3. 반환한 section의 모든 줄을 EvidenceRef로 기록한다.
4. sheet/month/section 선택이 현재 요청자의 문서 집합 밖으로 넓어지지 않는다.
5. 잘림과 coverage를 답변 추적에 남긴다.

이 조건 없이 목차 파서만 가져오면 동명 문서의 다른 버전을 열거나, 모델이 못 본 뒤쪽
줄에 출처를 붙이는 오류가 생긴다. 따라서 이번 변경에는 포함하지 않는다.

## 5. 검증 기준

- 문서 표식 줄과 사람 메시지가 별도 구역에 나온다.
- 일부 낱말만 맞은 결과가 완전 일치처럼 보이지 않는다.
- 모호한 채널 이름으로 어느 채널도 임의 선택하지 않는다.
- 채널 범위 검색은 전역 결과 상한이 차도 지정 채널 결과를 찾는다.
- 0건 낱말 진단도 지정 채널 밖 건수를 세지 않는다.
- hidden/private 문서, 봇 발언, TYBot 생성 Canvas가 새 경로로 노출되지 않는다.
- 출처는 모델 문장이 아니라 `Touched.evidence_hits`에서 계속 생성된다.

## 6. 구현·검증 기록

TYBot Archive Specialist에는 Hermes 런타임을 복사하지 않고 다음 행동만 반영했다.

- 문서·첨부·Canvas와 사람 대화 검색 결과 분리
- 부분 일치 결과에 일치 낱말 수 표시
- 정확 또는 유일한 채널 이름만 범위로 허용
- 권한 내 채널을 먼저 좁힌 뒤 검색
- 0건 낱말 진단도 지정 채널 범위 안에서만 집계
- 구체적인 표기 단서가 있을 때만 별칭 검색 1회 허용

변경 파일:

- `subbots/hermes/contract/prompt.md` — TYBot Archive Specialist 계약 v5
- `src/tybot/archive/store.py`
- `src/tybot/specialist_tools.py`
- `tests/test_specialist_tools.py`

검증 결과:

```text
관련 검색·권한 테스트 76 passed
ruff check src tests scripts: passed
Hermes src 전체 node --check: passed
전체 pytest: 2604 passed, 30 failed
```

전체 실패 30건은 Windows의 `bash.exe`가 실제 Linux bash가 아니라 WSL 미설치 안내를
반환해 specialist container 배포 스크립트 검사가 실패한 것이다. 변경한 검색·권한
경로의 실패는 없으며, Linux에서 해당 배포 테스트를 다시 실행해야 전체 녹색 판정이다.

## 7. 우리 측 이관 책임

프금팀 인계서의 코드 변경과 별개로 서버 소유자인 우리가 다음을 준비한다.

### 7.1 운영 계약

| 항목 | 결정 주체 | 결정 내용 |
|---|---|---|
| 운영 식별자 | 우리 | `pf-hermes`로 고정 |
| 업무 소유자 | 프금팀 | 정책 변경·서비스 중지 승인 담당자 |
| 인프라 소유자 | 우리 | 계정·시크릿·systemd·백업·복구 담당자 |
| 코드 릴리스 | 프금팀 | 고정 tag/commit, changelog, rollback 대상 |
| 자료 저장소 | 공동 | 소유자, deploy key, 보존기간, 충돌 대응 |
| Slack 앱 | 프금팀 | 앱 소유권, scope, token 교체 담당자 |
| 모델 비용 | 프금팀 | Anthropic 계정, 일일 상한, 경보 수신자 |
| RPO/RTO | 공동 | 자료 손실·복구 허용 시간 |

### 7.2 호스트·시크릿 격리

```text
user/group: pf-hermes
code:       /opt/tybot-subbots/pf-hermes/releases/<commit>/
current:    /opt/tybot-subbots/pf-hermes/current
state:      /var/lib/tybot-subbots/pf-hermes/
secrets:    /etc/tybot-subbots/pf-hermes.env
runtime:    /run/tybot-subbots/pf-hermes/
```

- 서비스 계정은 `/opt/tybot`, `/var/lib/tybot`, `/etc/tybot`, DB socket을 읽지 못한다.
- Slack, Anthropic, 코드 Git, 자료 Git 자격을 TYBot과 분리한다.
- 코드 Git은 read-only, 자료 Git은 별도 read/write deploy key를 쓴다.
- Node 20, SELinux, CPU·메모리·PID·journal 상한과 state 백업을 준비한다.
- 시크릿 원문을 콘솔·로그·감사 DB에서 조회할 수 없게 한다.

### 7.3 배포 helper

운영 서버에서 `git pull && npm install && restart`를 직접 실행하지 않는다. 고정 helper가
승인 commit 검사 → 검역 clone → lockfile 검사 → 시크릿 없는 build/test → 읽기 전용
release 생성 → smoke → `current` symlink 원자 전환 → 해당 unit 재시작 → 실패 시 이전
symlink 복원의 순서만 수행한다.

helper는 임의 command, 경로, unit과 환경변수를 인자로 받지 않는다. `pf-hermes`와
승인 action만 allowlist로 받고, 배포 중복 lock과 append-only 감사를 남긴다.

### 7.4 관측과 백업

Hermes가 다음 비민감 상태를 state 아래 JSON으로 원자적 기록하도록 프금팀과 계약한다.

- source commit, 시작 시각, 마지막 Slack 연결
- 마지막 수집과 archive pull/push, 미push commit, conflict
- 마지막 digest와 모델 호출, 당일 호출·토큰·비용
- 정규화된 오류 코드와 발생 시각

질문·답변·문서 본문·token·private channel 이름은 상태 파일에 넣지 않는다. 원격 Git만
백업으로 간주하지 않고 state 복구 훈련을 별도로 수행한다.

## 8. `/pf/` 운영 콘솔

### 8.1 결정

같은 React 디자인과 공통 컴포넌트는 재사용하되 PF 콘솔은 별도 backend로 둔다.

```text
https://<사내콘솔>/      -> tybot-console.service
https://<사내콘솔>/pf/  -> pf-hermes-console.service
```

단순 React route 추가는 격리가 아니다. 현재 TYBot 콘솔은 `developer`가 TYBot 전문 봇·로그·
규칙을 볼 수 있고 프로세스가 `/etc/tybot/tybot.env`와 `/var/lib/tybot`에 접근한다. 기존
`/api/specialist-runtime`도 TYBot이 호출하는 무시크릿 HTTP 전문가용이라 자체 Slack 앱과
Git 아카이브를 가진 Hermes에 재사용하지 않는다.

PF backend는 localhost 또는 Unix socket에만 bind하고 nginx/VPN이 `/pf/`로 proxy한다.
별도 외부 포트를 열지 않는다. B-35 TLS와 secure cookie가 PF 계정 추가보다 먼저다.

### 8.2 인증·DB

- PF 세션 cookie는 별도 이름과 `Path=/pf`를 사용한다.
- 회사 계정 원본은 공유할 수 있지만 서비스 권한은 `console_user_service`로 분리한다.
- 역할은 `viewer`, `operator`, `developer`, `approver`다.
- 개발자는 자기 release를 승인할 수 없다.
- PF DB role은 인증 최소 열, service scope, `managed_service_*`, 감사 append만 접근한다.
- TYBot 질문·원문·workspace secret·specialist 표에는 DB 권한이 없다.
- TYBot admin도 PF service 권한 행이 없으면 PF 데이터를 볼 수 없다.

### 8.3 화면·동작

첫 화면은 개요, 수집/Git, 배치, 배포, 비용, 감사로 제한한다. 만들지 않는 것은 임의 shell,
env 원문 편집, secret 조회, 아카이브 편집, 자동 Git merge/force push, TYBot 관리 화면이다.

허용 API는 `/pf/api/` 아래 고정 action만 둔다. 상태 조회 후 `health-check`, `archive-sync`,
`restart`, `stop`, release 제출·분리 승인·activate·rollback을 단계적으로 연다. request body로
service key나 unit 이름을 받아 범위를 바꾸지 않는다.

## 9. 구현 순서

1. **이관 기반**: 소유자·RPO/RTO 확정, 전용 계정·경로·시크릿·백업·systemd·수동 runbook
2. **수동 shadow**: 고정 commit과 archive 복제, 쓰기 비활성 비교, 단일 인스턴스 전환
3. **읽기 전용 콘솔**: TLS, 별도 backend·DB role·cookie·RBAC, 상태·비용·감사 화면
4. **제한 운영**: health, archive sync, restart, stop과 lock·timeout·감사
5. **배포 자동화**: 검역·build/test·분리 승인·원자 활성화·rollback

콘솔 버튼보다 안전한 systemd·helper·상태 계약이 먼저다. 실패한 후보 배포가 현재 Hermes나
TYBot을 중지해서는 안 된다.

## 10. 완료 게이트

- Hermes 계정이 TYBot env/archive/DB를 읽지 못한다.
- PF 사용자가 TYBot API와 DB에 접근하면 모두 거부된다.
- PF 콘솔과 서비스 로그에 시크릿·질문·답변·문서 본문이 없다.
- 같은 Slack 앱 token의 Hermes 프로세스가 동시에 두 개 뜨지 않는다.
- 코드와 자료 Git의 key·경로·장애 상태가 분리된다.
- Git, Slack, 모델, config와 archive 장애가 다른 error code로 보인다.
- rollback에 원문 삭제나 force push가 필요 없다.
- GCP 종료 전 24시간 shadow 비교와 실제 rollback 훈련을 완료한다.

## 11. PF 콘솔 구현 인계

Claude에게 이 문서만 막연히 읽으라고 하면 범위가 너무 넓다. 다음 문서와 범위를 함께
지정한다.

1. 이 문서 §7~§11: 인프라 경계, `/pf/` 구조, 구현 순서와 이관 일정
2. [`pf-hermes-migration-handoff.md`](pf-hermes-migration-handoff.md) §3.6, §5, §8~§9:
   health 계약, 인수 테스트, 프금팀 제출물
3. [`console.md`](console.md): 현재 TYBot 콘솔의 인증·감사·배포 구조
4. `BACKLOG.md` B-35와 B-64: TLS 선행 조건과 미완료 범위

첫 구현 범위는 **읽기 전용 PF 콘솔**로 제한한다. React 디자인과 공통 컴포넌트는 재사용할
수 있지만 backend 프로세스, env, 세션 cookie, DB role, 감사 범위는 TYBot과 분리한다.
`restart`, `stop`, archive sync, release activate와 rollback 버튼은 고정 helper와 감사 계약이
구현된 뒤에만 연다. 임의 명령, env 원문, secret, 질문·답변·문서 본문은 화면이나 API로
노출하지 않는다.

PF 콘솔은 2026-09-22에 구현·배포하고, 이관은 2026-09-23에 진행한다. 오늘 완료 범위는
B-35 TLS, 별도 PF backend·service·cookie·권한, 읽기 전용 상태·비용·감사 화면이다. 실제
Hermes 상태 파일이 아직 없는 개발 환경에서는 고정 fixture로 UI를 검증하고, 서버에서는
빈 상태와 unavailable 상태가 오류 없이 보여야 한다.

콘솔 구현 성공을 다음 날 이관의 유일한 선행 조건으로 만들지는 않는다. 오늘 배포 검증이
실패하거나 B-35 TLS가 완료되지 않으면 `/pf/`를 사용자에게 열지 않고, 내일 이관은 SSH와
승인된 수동 runbook으로 진행한다. 미완성 콘솔을 시간에 맞춰 공개하는 것보다 콘솔 없이
이관하는 편이 안전하다.

### 11.1 2026-09-22 콘솔 구현 순서

| 순서 | 작업 | 오늘 완료 기준 |
|---|---|---|
| 1 | B-35 TLS와 reverse proxy | 외부 `8787` 차단, HTTPS 로그인, `Secure` cookie 확인 |
| 2 | PF backend 분리 | `pf-hermes-console.service`, 별도 env·cookie·DB role, localhost/Unix socket bind |
| 3 | 읽기 전용 API | health·Git·배치·비용·감사 상태만 반환, secret·본문·TYBot 데이터 접근 거부 |
| 4 | `/pf/` UI | 개요·수집/Git·배치·비용·감사 화면과 unavailable/empty/error 상태 구현 |
| 5 | 자동 검사 | 인증·service scope·TYBot API 차단·cookie Path·민감정보 비노출 테스트 |
| 6 | 서버 배포 smoke | PF 계정 로그인, `/pf/` 새로고침, 로그아웃, TYBot 콘솔 회귀 확인 |

오늘 범위에 `restart`, `stop`, archive sync, release activate, rollback 버튼을 억지로 넣지
않는다. 고정 helper, 중복 실행 lock, timeout, append-only 감사가 구현된 기능만 이후에 연다.

## 12. 2026-09-23 이관·시험 일정

### 12.1 실현 가능성 판정

| 목표 | 판정 | 이유 |
|---|---|---|
| 09월 23일 12:00까지 격리 설치와 shadow 기동 | 조건부 가능 | 아래 사전 조건이 08:00 전에 모두 충족돼야 한다 |
| 09월 23일 13:00부터 기능·장애 시험 | 가능 | 오전 변경 동결과 점심시간 중 상태 유지가 전제다 |
| 09월 23일 12:00까지 기존 GCP 종료와 운영 전환 | 불가 | 완료 게이트의 24시간 shadow 관찰과 rollback 훈련을 충족하지 못한다 |
| 09월 24일 13:00 이후 운영 전환 | 조건부 가능 | 24시간 관찰, 대조, rollback 시험이 모두 통과해야 한다 |
| 09월 22일 읽기 전용 `/pf/` 콘솔 구현·배포 | 조건부 가능 | B-35 TLS와 별도 backend·인증·권한을 함께 완료해야 한다 |
| 09월 22일 PF 운영 제어·배포 자동화까지 완료 | 불가 | 고정 helper·lock·감사·rollback 검증이 없어 오늘 열면 안 된다 |
| 09월 23일 이관에서 검증된 `/pf/` 상태 화면 사용 | 조건부 가능 | 오늘 TLS·격리·회귀 검사가 통과한 경우에만 사용한다 |

08:00 go/no-go 전에 다음이 준비되지 않으면 당일 목표를 **설치 검증만**으로 낮춘다.

- 프금팀이 제출한 고정 tag/commit과 lockfile, Rocky 실행 검사 결과
- `pf-hermes` 전용 Slack 앱 token, Anthropic key, 코드·자료 Git deploy key
- env/config 변수 목록과 값 보관 위치. 값 자체는 문서나 채팅에 붙이지 않는다
- 전용 계정·경로·SELinux·resource limit·journal 정책과 state 백업 위치
- 자료 Git 최신 push 확인과 이전 GCP의 실행 commit·마지막 event timestamp 기록
- shadow에서 답변·수집·Git push를 막는 설정 또는 별도 시험용 Slack 앱
- 기존 GCP 복귀 권한을 가진 운영자와 검증된 rollback 명령

### 12.2 오전 이관 시간표

| 시간 | 작업 | 통과 기준 | 실패 시 |
|---|---|---|---|
| 08:00~08:20 | go/no-go, 담당자·시크릿·고정 commit·백업 확인 | 사전 조건 전부 확인, 변경 기록 시작 | 이관 중단, 누락 항목 보완 |
| 08:20~09:00 | 전용 계정·경로·권한·SELinux·resource limit 검증 | `pf-hermes`가 TYBot env/archive/DB를 읽지 못함 | 권한 수정 후 재검사 |
| 09:00~09:35 | 고정 release 배치와 의존성·offline 검사 | lockfile 불변, build/test 및 config fail-closed 통과 | release 폐기, 이전 상태 유지 |
| 09:35~10:10 | 자료 Git clone, 무결성·최신 commit 확인 | GCP와 archive commit 일치, 원문 수 임의 삭제 없음 | Git 문제 분리, 서비스 미기동 |
| 10:10~10:40 | env/config와 systemd preflight | secret 로그 없음, 쓰기 경로가 state 아래로 제한됨 | unit 시작 금지 |
| 10:40~11:10 | 답변·수집·push 비활성 shadow 기동 | health 2초 이내, Slack·Anthropic·Git 상태 구분 | 즉시 정지하고 로그 보존 |
| 11:10~11:40 | ACL·로그·중복 인스턴스·TYBot 영향 점검 | TYBot 무영향, 같은 token 동시 실행 없음 | shadow 종료, 롤백 |
| 11:40~12:00 | 오전 증적 정리와 변경 동결 | 13시 시험 대상 commit·설정 hash 확정 | 오후 시험 연기 |
| 12:00~13:00 | 변경 금지·상태 관찰 | 예기치 않은 재시작·쓰기 없음 | 13시 시험 취소 |

shadow에 기존 GCP와 같은 Slack token을 사용해서는 안 된다. 같은 token밖에 없다면 오전에는
offline/replay 시험까지만 수행하고, 실제 Slack 연결은 운영 전환 창에 기존 프로세스를 정지한
뒤 한 번만 연다.

### 12.3 13시 이후 시험 시간표

| 시간 | 시험 | 통과 기준 |
|---|---|---|
| 13:00~14:00 | 공개·비공개 채널, 사용자 권한, 질문·답변 smoke | GCP 기준과 권한·응답 동작 일치, 중복 답변 없음 |
| 14:00~15:00 | archive pull/push, 수집·digest, 재시작 | 중복 ingest·digest 없음, Git commit 수 설명 가능 |
| 15:00~16:00 | Slack 재연결, Anthropic 장애, Git pull/push·conflict | 장애별 error code와 degraded 상태가 구분됨 |
| 16:00~16:30 | rollback 훈련 | 새 서비스 정지와 GCP 복귀가 원문 삭제 없이 성공 |
| 16:30~17:00 | 비용·응답시간·수집 건수 기준선 기록 | 다음 날 비교할 수 있는 비민감 증적 완성 |
| 09월 23일 10:40~09월 24일 11:40 | 시험 시간을 포함한 24시간 이상 shadow 관찰 | crash, 비정상 쓰기, 권한 차이, 비용 이상 없음 |
| 09월 24일 13:00 이후 | 별도 승인 후 운영 전환 | 기존 GCP 정지 → 동일 token 프로세스 0개 확인 → 새 서비스 시작 |

12:00의 산출물은 운영 전환된 서비스가 아니라 **검증 가능한 shadow release와 오전 증적**이다.
실제 전환은 24시간 관찰 결과를 사람이 승인한 뒤 별도 변경 창에서 수행한다.

프금팀 AI 개발 에이전트에게 전달할 유일한 문서는
[`pf-hermes-migration-handoff.md`](pf-hermes-migration-handoff.md)다.
