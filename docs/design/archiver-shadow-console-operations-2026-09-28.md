# Archiver Shadow 연결 및 콘솔 운영 사양

> **후속 결정 (2026-09-29):** workspace별 systemd/env 수동 운영은 shadow 초기 파일럿에만
> 사용한다. 최종 구조는 단일 supervisor, 공통 shadow root, 콘솔 desired state, cursor 기반
> 소급 수집으로 변경됐다. 구현자는
> [Archiver 단일 Supervisor·콘솔 운영·소급 수집 작업지시서](archiver-supervisor-backfill-console-2026-09-29.md)를
> 우선 적용한다. 이 문서의 workspace별 unit과 중복 root는 이행 전 현행 절차를 설명할 뿐
> 최종 목표가 아니다.
> **2026-09-30 경로 결정:** 새 shadow 자료는
> `/var/lib/tybot/archiver-shadow/<workspace>/<channel-id>__<first-name>/`
> 아래 `archive/`, `objects/`, `staging/`로 나눈다. 세부 계약은 후속 문서 §1·§5를
> 따른다. 이 결정은 기존 shadow 폴더를 지금 삭제하라는 지시가 아니다.

작성: 2026-09-28  
대상: Claude Code 구현 담당자 및 TYBot 콘솔 검토자  
상태: Archiver credential reader 전환 구현·서버 배포 대기. 우선순위는 shadow 파일럿이며,
이 문서만으로 운영 서비스나 `active` 수집을 켜지 않는다.

## 1. 목표

먼저 TYIT pilot에서 Archiving Bot을 별도 Slack 앱으로 연결하고, 운영 아카이브와 분리된
shadow 경로로 수집을 검증한다. 콘솔의 systemd 시작/중지 UI는 파일럿 성공 후 진행하는
후속 편의 기능이다.

이 변경은 서비스 설치와 기동 절차를 줄이는 작업이다. 과거 메시지 백필, 기존 운영
아카이브 승격, Master 수집 중단, 채널 writer 인수는 포함하지 않는다.

## 2. 현재 구현에서 확인된 사실

- `console-web/src/pages/Bots.tsx`의 새 연결 화면은 `bot_connection`과
  `bot_connection_secret`을 사용한다.
- `src/tybot/archiver_runtime_store.py`는 새 연결 정본을 제한적으로 노출하는
  `archiver_connection_config()`에서 Archiver 토큰과 Master 신원을 가져온다.
- Master 연결과 전문 봇 라우팅 reader는 아직 옛 정본을 읽는다. 따라서 콘솔은
  `Archiver 연결만 다음 기동에 적용`된다고 구분해서 표시한다.
- `deploy/tybot-archiving-shadow@.service`는
  공통 `/etc/tybot/archiver.env`가 있어야 시작하며, 그 파일에서 DB 연결과 암호화 키를
  읽는다. 워크스페이스는 unit의 `%i`로 고른다. unit은 `[Install]`이 없는 수동 시작 전용이다.
- `src/tybot/archiving_bot.py`는 지정된 채널 ID의 Slack `message` 이벤트를 받는
  Socket Mode collector다. 서비스 시작은 과거 메시지를 자동 백필하거나 모든 첨부 변환이
  끝났다는 뜻이 아니다.
- Shadow archive root와 live `ARCHIVE_DIR`은 서로 포함되거나 같으면 안 된다.
- 채널 allowlist는 이름 규칙이 아니라 채널 ID로 제한한다. 사용자가 명시적으로 선택하고
  Archiver 앱을 초대한 채널이면 Master의 채널 네이밍 규칙에 맞지 않아도 대상이 될 수 있다.

따라서 현재는 **콘솔 연결 저장 → systemd 시작**만으로 완료되지 않는다. reader 전환이
포함된 서버 배포, 공통 Archiver env bootstrap, 채널 shadow allowlist가 준비돼야 한다.

## 3. 운영·개발 경계

파일럿에서는 shadow만 허용한다.

| 화면 상태 | 동작 | 저장 경로 | Master 수집 |
|---|---|---|---|
| Shadow 준비 | 토큰 등록 시 연결 자동 확인 | 아직 쓰지 않음 | 계속 운영 |
| Shadow 실행 | Archiver가 허용 채널 이벤트를 별도 수집 | `/var/lib/tybot/archiver-shadow/<workspace>/archive` | 계속 운영 |
| 중지 | Archiver 프로세스 중지 | 기존 shadow 자료 유지 | 계속 운영 |

- 운영/개발을 동시에 체크하는 두 개의 독립 체크박스는 만들지 않는다.
- 현재 콘솔에는 `운영 아카이브(live)` 선택이나 live 경로 입력을 제공하지 않는다.
- Shadow와 live는 서로 다른 정본이다. Shadow 결과를 기존 경로로 자동 복사·병합하지 않는다.
- `active`, `writer_owner=archiver`, Master writer 중지는 이 사양의 UI/API/helper에서
  수행하지 않는다. 별도 release gate 및 인수 절차가 필요하다.
- 연결 자격 증명 검증과 프로세스 시작은 별도 상태/동작이다. 토큰 저장이나 Slack
  `auth.test` 성공만으로 collector가 시작되면 안 된다.

## 4. 목표 사용자 흐름

1. `봇 관리 > 워크스페이스 연결`에서 TYIT의 Archiver Slack Bot Token과 App-Level Token을
   등록한다. 같은 요청에서 bot token과 app token을 Slack에 확인하고, team ID와 bot user
   ID까지 기록한다. 별도의 `신원 확인` 버튼이나 후속 절차는 두지 않는다. 평문은 응답에
   되돌려 주지 않고 저장 뒤 입력 필드를 비운다.
2. 연결 상세에서 해당 봇의 Slack App Manifest 정본을 바로 열고 복사할 수 있어야 한다.
   별도 Manifest 탭을 찾아가야만 설치 파일을 확인할 수 있게 하지 않는다. 관리자는 표시된
   SHA-256과 실제 Slack 앱 설정을 대조한 뒤 확인 기록을 남긴다.
3. 관리자가 채널 목록에서 최대 5개의 채널 ID를 선택한다. 이름은 표시용일 뿐 식별키가
   아니다. private channel은 Archiver 앱 초대 및 실제 권한이 확인되지 않으면 시작을 막는다.
4. 서버 사전조건이 준비되지 않은 경우 콘솔은 구체적인 누락 항목만 표시하고 시작을
   비활성화한다. 예: 런타임 credential reader 미배포, DB bootstrap 누락, shadow 경로
   권한 오류, 토큰 등록 시 자동 연결 확인 실패, 채널 0개 또는 5개 초과.
5. 준비 완료 후 승인된 운영자가 `Shadow 수집 시작`을 누른다. 확인 창에는 workspace,
   선택 채널 수, shadow 경로, live archive를 수정하지 않는다는 점을 보여 준다.
6. 시작 결과와 systemd 상태를 표시한다. 실제 Slack 이벤트가 들어와 파일이 기록되기 전에는
   `수집 완료`라고 표시하지 않는다.
7. 사용자는 shadow archive의 건수/최근 이벤트/최근 오류를 확인하고 Master 운영본과
   비교한다. 비교 결과를 확인하기 전에는 기존 수집을 끄지 않는다.

### 4.1 여러 워크스페이스 등록과 파일럿 순서

- 같은 Archiver Manifest를 사용해 `tyit`, `pilot` 등 여러 워크스페이스에 앱을 미리 만들고
  토큰을 등록할 수 있다. 연결 확인은 등록 요청에 포함된다.
- Slack 앱과 Bot/App Token은 워크스페이스마다 별도다. 한 워크스페이스의 토큰을 다른
  워크스페이스 연결에 재사용하지 않는다.
- 여러 연결을 등록했다고 여러 collector를 동시에 시작하지 않는다. 최초 shadow 기동은
  `tyit` 한 workspace, 1~3개 채널로 제한한다.
- `tyit`에서 경로 격리, 이벤트 수집, 수정·삭제, 첨부, 운영 archive 불변 검증을 통과한
  뒤에만 다음 workspace를 순차적으로 시작한다.

### 4.2 연결 상세의 봇별 Manifest

각 `봇 관리 > 워크스페이스 연결` 행 또는 상세에는 연결 대상 봇의 Manifest를 표시한다.

| 봇/연결 | Manifest 정본 | 화면 동작 |
|---|---|---|
| Master / Slack 직접 | `docs/pilot/slack-app-manifest.yaml` | 보기·복사·SHA-256 대조 |
| Archiver / Slack 직접 | `docs/pilot/archiving-app-manifest.yaml` | 보기·복사·SHA-256 대조 |
| Hermes / Slack 직접 | PF가 제출하고 승인한 정본 | 제출 전에는 `정본 미등록`, 임의 생성 금지 |
| Hermes·Clio / Master 내부 호출 | Slack Manifest 없음 | `내부 호출 연결`로 표시, 토큰/Manifest 입력 없음 |

- Manifest YAML은 시크릿이 아니므로 승인된 콘솔 사용자에게 내용을 보여 줄 수 있다.
- API는 catalog에 등록된 Manifest ID만 읽는다. 요청자가 파일 경로나 임의 Manifest 내용을
  전달하게 하지 않는다.
- Manifest 상세 응답은 `manifestId`, `botKey`, `sourcePath`, `sha256`, `content`를 포함할
  수 있다. `content`는 등록된 저장소 파일을 UTF-8로 읽은 값이어야 한다.
- `Manifest 보기`는 설치 편의 기능이고, `Manifest 적용 확인`은 사람이 Slack 설정과
  SHA-256을 대조했다는 감사 기록이다. 보기/복사만으로 확인 상태를 자동 변경하지 않는다.
- Token identity 검증과 Manifest 대조는 별도 상태로 유지한다.

## 5. 연결 정본과 런타임 이행

### 5.1 단일 정본

- 새 콘솔과 런타임은 `bot_connection` + `bot_connection_secret`을 Archiver Slack 앱
  연결의 정본으로 사용한다.
- 새 경로와 `workspace_service`에 토큰을 계속 이중 기록하지 않는다. 이중 기록은 전환
  경로를 모호하게 하므로 금지한다.
- 기존 `workspace_service` 자료가 운영 중이면 migration/전환 도구로 보존하고, 새
  reader 배포 전까지 기존 runtime을 깨뜨리지 않는다. reader cutover와 기존 경로 정리는
  별도 커밋 및 검증으로 진행한다.
- 성공적인 토큰 확인 결과는 해당 연결 ID와 토큰 세대/fingerprint에 묶인다. 토큰 교체
  후에는 기존 신원 확인을 무효화한다.

### 5.2 Runtime configuration

Archiver 시작 시 workspace별로 다음 값을 검증해 가져온다.

- 승인된 Slack connection 및 암호화 토큰
- 동일 workspace의 Master identity 비교에 필요한 bot user ID
- 최대 5개의 명시적 channel ID allowlist
- `shadow` 모드와 workspace의 고정 shadow archive root
- 분리 첨부 설정은 현재 비활성. 이 사양에서 켜지 않는다.

DB 장애, 연결 비활성/미검증, 누락된 identity, 범위 초과 allowlist, 잘못된 경로 중 하나라도
있으면 fail closed로 시작을 거절한다. 오류 메시지와 로그에는 토큰, DSN, 암호화 키,
메시지 원문을 넣지 않는다.

## 6. 콘솔의 서비스 운영 기능

### 6.1 화면 위치와 내용

`봇 관리 > 워크스페이스 연결 > Archiver` 상세에 `Shadow 실행` 영역을 둔다. 워크스페이스
관리의 기존 `ArchivingPanel`은 채널 mode, writer owner, ACK, revision, 보존 설정을 계속
소유하고, Slack 토큰 입력이나 systemd 임의 제어를 맡지 않는다.

연결 상세 상단에는 해당 봇의 `Manifest 보기` 동작을 둔다. 별도 `Slack 앱 Manifest` 탭은
전체 catalog와 일괄 대조 이력 확인용으로 유지하되, 일상적인 앱 설치를 위해 반드시 그 탭을
거치게 만들지는 않는다.

표시할 상태:

- `서버 준비 필요`: unit/bootstrap/env/root 준비가 안 됨
- `연결 확인 필요`: token/identity가 없거나 stale
- `중지됨`
- `시작 중`
- `Shadow 수집 중`: systemd active 및 runtime health 확인
- `오류`: 오류 종류와 안전한 로그 tail 제공
- `최근 이벤트 없음`: 프로세스는 살아 있으나 실제 Slack 이벤트 수집은 확인되지 않음

제공할 동작은 workspace-scoped `준비 상태 확인`, `Shadow 시작`, `Shadow 중지`,
`상태 새로고침`, `최근 로그 보기`로 제한한다. 자동 재시작은 systemd 정책에 맡긴다.
UI에서 임의 경로, unit 이름, shell command, 환경변수, 로그 경로를 받지 않는다.

### 6.2 특권 경계

- 배포 과정에서 root 소유 unit template을 설치하고 `systemctl daemon-reload`를 한 번
  수행한다. 일반 workspace 추가/시작 때마다 unit 파일을 쓰거나 daemon-reload 하지 않는다.
- 콘솔은 root로 실행하지 않는다. 일반 서비스 계정에 임의 `systemctl` 권한을 주지 않는다.
- root 소유의 고정 helper(예: `/usr/local/libexec/tybot-archiver-control`)를 설치하고,
  sudoers에는 해당 helper만 허용한다. helper는 `status|start|stop|logs`와 검증된
  workspace key만 받는다.
- helper는 workspace key를 엄격한 slug allowlist로 검증하고 unit 이름과 경로를 자체
  생성한다. 요청자가 전달한 unit/path/명령을 그대로 실행하지 않는다. 허용 대상은
  `tybot-archiving-shadow@<workspace>.service`로 고정한다.
- helper는 live TYBot unit, timer, 다른 workspace 서비스, `daemon-reload`, enable,
  임의 파일 읽기/쓰기, 셸 실행을 제공하지 않는다.
- Backend는 요청자 역할, 연결의 identity 확인 시각, shadow 채널 설정, feature gate,
  서버 bootstrap 상태를 확인한 뒤 helper를 호출한다. helper 실패/timeout도 감사 기록에
  남긴다.
- 모든 시작/중지 요청에는 사유를 요구하고, actor/workspace/action/result/time을 감사한다.
  비밀과 원문은 감사 payload에서 제외한다.
- helper 출력은 알려진 상태 코드만 반환한다. journal은 제한된 최근 N줄만 읽고 토큰,
  URL credential, 원문이 노출되지 않도록 필터링한다. 필터링을 보장할 수 없으면 로그 보기
  기능은 제공하지 않고 서버 운영 절차로 남긴다.

### 6.3 서버 bootstrap

현재 systemd unit은 공통 `/etc/tybot/archiver.env`를 요구한다. 이 파일에는 Slack
토큰이나 `ARCHIVER_WORKSPACE`가 아니라 Archiver 전용 DB 연결 및
`WORKSPACE_SECRET_KEY` 등 공통 bootstrap 값만 있어야 한다. 워크스페이스는 unit의
`ARCHIVER_WORKSPACE=%i`로 선택한다.

- 이 bootstrap 비밀은 기존 승인된 서버 secret provisioning 절차로 설치한다. 브라우저,
  API request, 콘솔 DB, 감사 로그, process argv로 전달하지 않는다.
- 콘솔은 비밀값을 만들거나 표시하지 않는다. 파일이 없거나 권한이 틀리면 `서버 준비 필요`를
  반환한다.
- 목표 상태는 이 파일의 DB 연결 정보도 systemd credential/secret manager로 옮기는 것이다.
  그 전에는 기존 보호된 env file 절차를 유지하되, 콘솔에서 편집 기능을 만들지 않는다.
- workspace shadow root는 helper 또는 별도 배포 bootstrap에서 고정 prefix 아래 만들고
  owner/mode를 검사한다. symlink, `..`, prefix 밖 경로는 거부한다.

## 7. API 계약 초안

필요한 API는 기존 인증·권한·감사 middleware를 재사용한다.

- `GET /api/bots/connections/{workspace}/archiver/runtime`: runtime effect, 준비 조건,
  service state, last event/error metadata를 반환한다. 토큰은 반환하지 않는다.
- `GET /api/bot-manifests/{manifest_id}`: catalog에 등록된 Manifest 정본의 내용과
  SHA-256을 반환한다. 임의 파일 경로는 받지 않는다.
- `POST /api/bots/connections/{workspace}/archiver/runtime/preflight`: 읽기 전용 검사.
- `POST /api/bots/connections/{workspace}/archiver/runtime/start`: 사유 필수, shadow만 시작.
- `POST /api/bots/connections/{workspace}/archiver/runtime/stop`: 사유 필수, 해당
  workspace shadow unit만 중지.
- `GET /api/bots/connections/{workspace}/archiver/runtime/logs`: 제한된 안전 로그만 반환.

API는 상태 의미를 구분한다. `systemd active`는 프로세스가 실행 중임을 뜻할 뿐, Slack
이벤트가 수집·파일로 기록됐다는 증거는 아니다. 별도의 last-event/ACK 근거가 없으면
`수집 중` 대신 `프로세스 실행 중 · 수집 이벤트 대기`라고 표시한다.

실제 라우트/JSON 이름은 기존 `bot_routes.py`, response 모델 관례에 맞게 구현자가 조정할
수 있지만, 비밀 비노출과 workspace scope는 바꾸지 않는다.

## 8. 배포 문서와 사용자 안내 수정

`docs/deploy/archiving-shadow.md`를 새 연결 정본과 일치시킨다.

- 현재의 "Workspace 상세 > Archiving 서비스 연결" 안내를 봇 관리 연결 경로로 수정한다.
- 새 콘솔 연결이 런타임 reader에 적용되기 전에는 systemd start를 하지 말라고 명시한다.
- 수동 명령 경로는 긴급/최초 bootstrap fallback으로 유지하되, 구성 파일 생성이 별도
  필수라는 점을 앞에 둔다.
- unit 설치/daemon-reload는 배포 단계, workspace별 start/stop은 제한된 console action으로
  구분한다.
- 시작 성공이 과거 내역 백필, 첨부 전체 변환, 운영 검색 반영을 뜻하지 않는다고 명시한다.

## 9. 우선순위: 파일럿을 콘솔 제어 UI보다 먼저

### A. Shadow 수집 검증

1. 이미 적용된 DB schema와 Archiver credential reader 경계를 확인한다. 현재 reader가
   `workspace_service`만 읽는다면, **Archiver에 한정해** 새
   `bot_connection`/`bot_connection_secret` 정본을 읽도록 최소 전환한다. Master와 다른
   봇의 reader는 함께 바꾸지 않는다. 구형 연결 자료는 이행 및 롤백 검증 전 삭제하지 않는다.
2. 연결의 team/bot identity, Master와 다른 bot user, 채널 ID allowlist(최대 5개), DB
   권한, 첨부 분리 OFF, shadow/live 경로 비중첩을 검증하는 읽기 전용 preflight를 만든다.
3. deploy 단계에서 static unit template을 설치하고, 관리자가 별도로 준비한 DB bootstrap
   env file 및 shadow 디렉터리의 존재/권한을 확인한다. Slack token은 env file에 넣지 않는다.
4. 우선 `tyit`의 1~3개 테스트 채널만 사용해 기존 문서의 제한된 수동 systemd 절차로
   shadow 서비스를 시작한다. 새 연결 reader가 배포되지 않았거나 검증되지 않았다면 시작하지
   않는다.
5. 서비스 active만으로 성공 판정하지 않는다. 실제 Slack 이벤트가 들어온 뒤 ACK/파일
   기록, 채널 ID, 시간, 결과 kind를 비민감 메타데이터로 확인한다. 메시지 원문은 로그나
   보고서에 복사하지 않는다.
6. live archive의 파일 hash/mtime이 변하지 않았는지, 새 기록이 shadow root 아래에만
   생겼는지 확인한다. Master는 계속 운영 writer로 둔다.
7. 결과와 한계(기동 이후 이벤트만, 채널 초대/권한, 최대 5채널, 첨부 변환 상태)를 정리한다.
   기준을 통과하기 전에는 콘솔 제어 UI나 추가 채널 확장으로 넘어가지 않는다.

### B. 파일럿 통과 후 콘솔 운영 UI

8. workspace-scoped runtime status/preflight API를 읽기 전용으로 제공한다.
9. root-owned unit을 deploy installer에 포함하고 static bootstrap check를 유지한다.
10. 제한된 root helper와 sudoers를 추가한다. 악성 workspace key, 다른 unit 이름, 임의
    argument, symlink/prefix 탈출이 거부되는지 시험한다.
11. 콘솔에 연결 상태와 runtime 상태/시작/중지/안전 로그를 구현하고 권한·감사·실패 경로를
    검증한다.

어느 단계에서도 live/archive writer 전환은 하지 않는다.

## 10. 완료 기준

- 새 Bot Management 연결을 저장하고 재기동한 Archiver가 그 연결을 사용한다. 옛 표에
  장기 dual-write하지 않는다.
- 기존 workspace의 Master와 Archiver 설정/동작이 회귀하지 않는다.
- 승인되지 않은 계정은 runtime 조작을 할 수 없고, 모든 조작이 감사된다.
- 콘솔로 제어 가능한 대상은 지정 workspace의 shadow unit뿐이다.
- arbitrary command, arbitrary unit/path, live unit 제어 경로가 없다.
- 실패한 preflight는 시작을 막고 원인을 비밀 없이 표시한다.
- 성공한 start는 shadow 경로만 쓴다. `/var/lib/tybot/archive`와 기존 TYBot timer에는
  영향이 없다.
- 이름 규칙 없는 채널도 ID allowlist와 Slack 권한이 충족되면 수집된다.
- PC 콘솔 화면과 키보드 흐름을 검증한다. 모바일 QA는 범위에서 제외한다.
- 단위/통합 테스트, ruff, 프런트엔드 build가 통과한다.
- `active`, writer handoff, 운영 아카이브 승격은 미구현/비활성으로 남는다.

## 11. Claude Code 작업 지시

이 사양을 기준으로 구현한다. 먼저 기존 dirty worktree와 다른 에이전트 변경을 확인하고,
그 변경을 되돌리거나 덮어쓰지 않는다. 구현은 작은 검토 가능한 커밋 단위로 나누되,
사람 승인 없이 commit/push하지 않는다.

서버에서 실행할 명령은 문서로 제안만 하고 직접 실행하지 않는다. live token 입력, 운영
DB migration, Master 수집 중지, `archiver_writes_live`, channel `active`, 첨부 분리,
writer handoff는 이 작업의 범위 밖이다.
