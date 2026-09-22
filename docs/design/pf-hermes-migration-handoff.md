# Hermes 서버 이관 준비 계약

작성: 2026-09-21  
독자: 프금팀 Hermes 개발 AI 및 운영 이관 담당자  
대상: 프금팀이 개발·운영하는 `Hermes`

이 문서에서 제품명은 `Hermes`로 표기한다. `pf-hermes`는 TYBot과 같은 서버에서
충돌 없이 운영하기 위한 서버 내부 식별자이며, Hermes의 공식 제품명이나 버전명이 아니다.

## 1. 확인된 현재 구조

Hermes는 현재 독립 Node.js Slack 봇이다. TYBot 내부의 기록 검색 전문가와는 다른
제품이다. 이 문서에서는 프금팀 제품만 `Hermes`라고 부른다.

| 항목 | 소스에서 확인한 현재 값 |
|---|---|
| 실행 위치 | Google Cloud VM `hermes`, project `whk-hermes`, zone `us-west1-b` |
| 코드 저장소 | 기본 예시 `git@github.com:wkimclementia/hermes.git` |
| 자료 저장소 | 기본 예시 `git@github.com:wkimclementia/hermes-archive-tec.git` |
| 서버 경로 | 코드 `/opt/hermes/code`, 자료 `/opt/hermes/archive` |
| 런타임 | Node.js 20+, Slack Bolt Socket Mode |
| 모델 | Anthropic API 직접 호출 |
| 서비스 | `hermes.service`, OS 계정 `hermes` |
| 자료 동기화 | 별도 Git 저장소를 15분마다 pull, 수집 시 commit/push |
| 설정 | 코드 쪽 `.env`와 자료 저장소의 `config.json` |

따라서 “개인 GCP에서 실행하고 개인 Git 저장소를 아카이브로 사용한다”는 이해가
소스와 일치한다. 저장소가 실제로 private인지 Git 호스팅 설정 자체는 스냅샷만으로
검증할 수 없지만, SSH deploy key와 write 권한을 전제로 한 배포 문서는 비공개 운영
형태를 가리킨다.

## 2. 목표 구조와 넘지 말아야 할 경계

프금팀 Hermes와 TYBot은 같은 Rocky 서버에 있어도 **별도 서비스**다. 소스, 환경,
Slack 토큰, 모델 키, 자료, 로그와 장애 범위를 공유하지 않는다.

```text
/opt/tybot/                              TYBot 릴리스, Hermes가 읽지 못함
/var/lib/tybot/                          TYBot 중앙 아카이브, Hermes가 읽지 못함
/etc/tybot/tybot.env                     TYBot 자격, Hermes가 읽지 못함

/opt/tybot-subbots/pf-hermes/code/  프금팀 Hermes 불변 릴리스
/var/lib/tybot-subbots/pf-hermes/   archive, cache, lock, runtime state
/etc/tybot-subbots/pf-hermes.env    프금팀 전용 시크릿
/etc/tybot-subbots/pf-hermes.json   비시크릿 운영 설정 또는 그 위치
```

권장 서비스 계정은 `pf-hermes`, unit 이름은 `pf-hermes.service`다. 기존의 일반 이름
`hermes.service`,
`/opt/hermes`, 사용자 `hermes`를 그대로 쓰면 향후 다른 부서 Hermes 및 TYBot의
운영 명칭과 충돌한다.

이 이관은 두 봇의 아카이브를 합치는 작업이 아니다. 프금팀 자료는 프금팀 서비스가
소유한다. TYBot 중앙 원문으로 옮기려면 별도의 B-29 데이터 이관 관문(봇 출력 제외,
기본 비공개, PII 선검사, 출처 좌표 보존)을 통과해야 한다.

## 3. Hermes 개발자가 다음 release에 반영할 변경

### 3.1 환경 파일을 명시적으로 선택한다

현재 `dotenv`가 작업 디렉터리의 `.env`를 암묵적으로 읽는 구조를 없애거나, 최소한
다음 우선순위를 구현한다.

1. `HERMES_ENV_FILE`이 있으면 그 절대경로만 읽는다.
2. 운영 모드에서 값이 없으면 시작을 실패한다.
3. 개발 모드에서만 저장소 루트 `.env`를 허용한다.
4. 이미 프로세스에 주입된 환경변수를 `.env` 값으로 덮어쓰지 않는다.

운영 unit은 다음처럼 전용 파일을 주입한다.

```ini
[Service]
Environment=NODE_ENV=production
Environment=HERMES_INSTANCE=pf
EnvironmentFile=/etc/tybot-subbots/pf-hermes.env
```

전용 env에 허용할 값:

```dotenv
SLACK_BOT_TOKEN=...
SLACK_APP_TOKEN=...
ANTHROPIC_API_KEY=...
HERMES_DATA_ROOT=/var/lib/tybot-subbots/pf-hermes/archive
HERMES_CONFIG_FILE=/etc/tybot-subbots/pf-hermes.json
HERMES_INSTANCE=pf
```

`GCP_VM`, `GCP_PROJECT`, `GCP_ZONE`은 런타임 필수값에서 제거한다. GCP 배포 확인은
GCP 전용 개발 스크립트의 선택 기능으로 남기고 Rocky 운영 health에 포함하지 않는다.

### 3.2 모든 쓰기 경로를 하나의 상태 루트 아래로 모은다

코드에서 로그, lock, cache, health, ingest 임시 파일과 대화 로그 경로를 찾아
`HERMES_STATE_ROOT` 또는 config의 명시적 절대경로로 받는다. 기본값으로 코드 저장소나
현재 작업 디렉터리에 쓰지 않는다. systemd의 `ReadWritePaths`는 아래 하나만 허용한다.

```ini
ReadWritePaths=/var/lib/tybot-subbots/pf-hermes
```

### 3.3 인스턴스 정체성을 외부화한다

서비스명, 로그 식별자, lock 이름, cron/timer 이름, 상태 파일과 Git commit author에
하드코딩된 `hermes`를 인스턴스 키로 분리한다. 최소 지원값은 다음과 같다.

- `HERMES_INSTANCE=pf`
- `HERMES_SERVICE_NAME=pf-hermes`
- `HERMES_DATA_ROOT`
- `HERMES_STATE_ROOT`
- `HERMES_CONFIG_FILE`

같은 호스트에 두 인스턴스를 띄우는 테스트에서 lock, 로그와 자료 경로가 겹치지 않아야
한다. 단, 같은 Slack 앱 토큰을 두 인스턴스에 넣는 구성은 지원하지 말고 시작 시 경고한다.

### 3.4 배포물을 Rocky 8/9용으로 분리한다

현재 setup은 Debian/GCP 전제를 갖는다. 기존 파일을 조건문으로 계속 늘리지 말고
다음 산출물을 별도로 제공한다.

- `deploy/rocky/pf-hermes.service`
- `deploy/rocky/pf-hermes-archive-pull.service`
- `deploy/rocky/pf-hermes-archive-pull.timer`
- `deploy/rocky/install.sh` 또는 사람이 검토 가능한 설치 체크리스트

운영 unit의 필수 보안 설정:

```ini
User=pf-hermes
Group=pf-hermes
WorkingDirectory=/opt/tybot-subbots/pf-hermes/code
ExecStart=/usr/bin/node src/index.js
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/tybot-subbots/pf-hermes
UMask=0077
```

서비스는 `/opt/tybot`, `/var/lib/tybot`, `/etc/tybot`에 읽기 권한을 받지 않는다.
새 인바운드 포트도 열지 않는다. Socket Mode와 Git/Anthropic을 위한 아웃바운드만 쓴다.

### 3.5 Git 아카이브 실패를 봇 실행 실패와 분리한다

자료 Git 인증은 코드 저장소 키와 분리된 read/write deploy key를 쓴다. pull/push
실패가 현재 메모리의 Slack 응답 프로세스를 죽이지 않아야 하며, 다음 상태를 구분한다.

- 로컬 수집 성공 / Git push 실패
- Git pull 충돌
- 원격 인증 실패
- 자료 저장소가 예상 commit보다 뒤처짐

충돌 시 자동 merge나 force push를 하지 않는다. ingest를 중지하고 health를 degraded로
표시한다. 원본과 봇 생성 요약이 같은 Git 이력에 있다면 경로와 파일 표식을 명시해
향후 데이터 이관 시 기계적으로 제외할 수 있어야 한다.

### 3.6 시작 전 구성 검증을 강화한다

`npm run check:offline`에서 실제 API 호출 없이 아래를 실패로 잡는다.

- env/config/state/data 경로가 서로 다른 인스턴스 경로인지
- env 파일 권한이 group/other readable이 아닌지
- Slack bot/app token과 모델 키가 비어 있거나 예시값인지
- 자료 저장소 쓰기 가능, 코드 저장소 쓰기 불필요
- TYBot 경로를 가리키는 설정이 하나라도 있는지
- 서비스 계정과 파일 소유자가 다른지

로그에는 값이 아니라 변수명과 설정 출처만 남긴다.

## 4. 서버 운영자가 준비할 것

1. 전용 OS 계정과 디렉터리를 만든다.
2. 검증된 tag/commit을 `/opt/tybot-subbots/pf-hermes/code`에 배포한다.
3. 전용 archive Git을 `/var/lib/tybot-subbots/pf-hermes/archive`에 clone한다.
4. 전용 env/config를 배치하고 mode `0600`, owner `root:pf-hermes`로 제한한다.
5. 기존 개인 GCP와 **다른 Slack 앱 토큰 세트**인지 확인한다.
6. offline check, archive check, Slack read-only smoke test 순서로 실행한다.
7. GCP 인스턴스를 먼저 끄지 말고 새 서버를 답변 비활성 shadow 모드로 검증한다.
8. 전환 시점에 기존 GCP 봇을 정지한 뒤 새 서비스를 시작한다. 같은 토큰의 동시 실행은
   중복 답변과 중복 수집을 만든다.

실제 시크릿과 clone URL은 이 문서나 TYBot 저장소에 기록하지 않는다.

## 5. 인수 테스트

- 전용 env 없이 서비스가 fail-closed로 종료된다.
- TYBot env만 존재하는 호스트에서도 Hermes가 그 값을 읽지 않는다.
- Hermes 계정으로 `/etc/tybot/tybot.env`와 `/var/lib/tybot/archive`를 읽지 못한다.
- 프금팀 공개/비공개 채널 권한 회귀가 기존 GCP 결과와 일치한다.
- archive pull 실패 중에도 기존 로컬 자료로 답하며 degraded 상태를 표시한다.
- Anthropic 장애, Slack 재연결, Git 충돌이 각각 다른 error code로 기록된다.
- 서비스 재시작 후 중복 digest, 중복 ingest, 중복 답변이 없다.
- 로그와 health 출력에 토큰, 질문 원문, 비공개 문서 본문이 노출되지 않는다.
- 24시간 shadow 관찰 후 비용, 응답시간, 수집 건수와 Git commit 수를 GCP와 대조한다.

## 6. 롤백

1. 새 서비스를 정지한다.
2. 새 서버에서 추가된 archive commit이 원격에 정상 push됐는지 확인한다.
3. 동일 토큰을 쓰는 프로세스가 0개인지 확인한 뒤 기존 GCP 서비스를 다시 시작한다.
4. DNS나 인바운드 전환은 없으므로 Slack 앱 설정을 되돌리지 않는다.
5. 실패 시점, 새 서버 commit, 마지막 Slack event timestamp를 남겨 중복 수집 범위를
   계산한다. 원문을 삭제해 맞추지 않는다.

## 7. 다른 제품과의 경계

같은 서버에는 TYBot이 운영하는 내부 기록 전문가도 있지만, 이 문서에서는 이를
**TYBot Archive Specialist**라고 부른다. 내부 등록 key가 우연히 `hermes`인 것뿐이며,
프금팀 Hermes의 프로세스·저장소·배포 대상이 아니다.

Hermes 개발자는 다음을 전제로 작업한다.

- TYBot 소스, 중앙 아카이브, DB, Slack 토큰을 읽거나 호출하지 않는다.
- Hermes의 Slack 수집, Git 아카이브, LLM, digest와 답변 동작은 Hermes 안에 유지한다.
- TYBot Archive Specialist의 계약이나 프롬프트를 수정하지 않는다.
- 두 제품 사이에 런타임 호출이나 데이터 동기화를 새로 만들지 않는다.

## 8. 프금팀 제출물

이관 검토를 요청할 때 다음을 하나의 불변 release tag 또는 고정 commit으로 제출한다.

1. 3장의 환경·상태·인스턴스 분리 변경
2. Rocky용 systemd unit과 설치 전 검사 스크립트
3. `package-lock.json`과 Node.js 지원 버전
4. offline/archive/live-read 검사의 실행 명령과 기대 결과
5. 민감정보 없는 health JSON 스키마와 원자적 기록 구현
6. GCP 전용 기능을 끈 Rocky 운영 설정 예시
7. 변경 파일 목록, 알려진 제한, 이전 release로 되돌리는 절차
8. 다음 실패를 구분하는 error code: Slack, Anthropic, Git pull, Git push, conflict,
   archive invalid, config invalid

health JSON에는 실행 commit, 시작 시각, 마지막 Slack 연결·수집·Git 동기화·digest 성공,
미push commit 수, 당일 모델 사용량과 오류 코드만 담는다. 토큰, 질문·답변, 문서 본문과
private channel 이름은 담지 않는다.

## 9. 완료 정의

프금팀 개발 작업은 다음이 모두 자동 검사될 때 완료다.

- 명시적 env/config/state 경로 없이 production 시작이 실패한다.
- 코드 디렉터리에 `.env`, 로그, lock, cache 또는 임시 파일을 쓰지 않는다.
- 두 인스턴스 fixture가 경로·lock·로그를 공유하지 않는다.
- TYBot 경로를 설정하면 offline check가 실패한다.
- health 생성은 Slack·Anthropic 호출 없이 2초 안에 끝난다.
- archive Git 장애가 프로세스 전체 종료나 force push로 이어지지 않는다.
- 같은 Slack token의 중복 인스턴스 가능성을 시작 전에 경고한다.
- 테스트와 로그가 시크릿 값을 출력하지 않는다.

서버 계정 생성, 시크릿 발급, 백업, `/pf/` 콘솔과 실제 전환은 서버 운영팀이 담당한다.
프금팀 release가 이 계약을 통과한 뒤 공동 shadow 검증 일정을 잡는다.
