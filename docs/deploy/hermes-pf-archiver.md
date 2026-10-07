# PF Hermes를 Archiver 정본에 연결하는 절차

상태: **운영 적용 전**. 이 문서는 TYBot 서버에서 PF Hermes를 별도 서비스로 실행하는
수동 전환 절차다. 설치 스크립트는 이 서비스를 자동으로 활성화하지 않는다.

## 배치 원칙

- Hermes 질문·DM·일간/주간 요약은 유지한다.
- 원문 수집·소급·첨부 저장은 Archiving Bot만 수행한다.
- Hermes는 `/var/lib/tybot/archive`를 읽기 전용으로만 연다.
- 기존 PF VM과 이 서비스를 동시에 실행하지 않는다.
- 토큰과 workspace 키는 저장소 밖 `/etc/hermes-pf/hermes.env`에 둔다.
- 인터넷 인바운드 포트는 열지 않는다. Slack Socket Mode와 모델 API로 나가는 연결만 쓴다.

## 1. 코드와 의존성 준비

아직 서비스를 시작하지 않는다.

```bash
id -u hermes >/dev/null 2>&1 || \
  sudo useradd --system --home-dir /var/lib/hermes-pf --shell /sbin/nologin hermes
node --version
cd /opt/tybot/subbots/hermes
sudo npm ci --omit=dev
sudo install -d -o hermes -g hermes -m 0700 /var/lib/hermes-pf
sudo install -d -o hermes -g hermes -m 0700 /opt/tybot/subbots/hermes/logs
```

`/var/lib/hermes-pf/config.json`에는 PF의 기존 `config.json`을 두되 다음을 확인한다.

- `privateChannels`가 채널 공개 여부 manifest와 일치한다.
- `digest.ingest.enabled`와 `digest.ingestPre.enabled`는 꺼져 있다.
- 질문·DM·`digest.daily`·`digest.weekly` 설정은 기존 PF 값이다.
- `archiver.root`는 환경변수가 덮어쓰므로 운영 경로를 중복 기입하지 않는다.
- `archiver.workspace`는 Slack 표시명이 아니라 Archiver workspace 키다.

## 2. 비밀 환경 파일

```bash
sudo install -d -o root -g hermes -m 0750 /etc/hermes-pf
sudo install -o root -g hermes -m 0640 /dev/null /etc/hermes-pf/hermes.env
sudoedit /etc/hermes-pf/hermes.env
sudo install -o root -g hermes -m 0640 /dev/null /etc/hermes-pf/privacy.env
sudoedit /etc/hermes-pf/privacy.env
```

값은 다음 네 개만 둔다. 실제 값은 문서나 셸 이력에 남기지 않는다.

```dotenv
SLACK_BOT_TOKEN=<secret>
SLACK_APP_TOKEN=<secret>
ANTHROPIC_API_KEY=<secret>
HERMES_ARCHIVER_WORKSPACE=invest
```

`privacy.env`에는 비밀값 없이 같은 workspace 키만 둔다. manifest exporter는 Hermes의
Slack·LLM 토큰을 읽지 않는다.

```dotenv
HERMES_PRIVACY_WORKSPACE=invest
```

`HERMES_MODE`, `HERMES_DATA_ROOT`, `HERMES_ARCHIVER_ROOT`는 unit이 고정한다.

## 3. 아카이브 읽기 권한

서비스 계정에 TYBot 그룹 전체 권한을 주지 않는다. 코드와 파일럿 워크스페이스만 ACL로
연다. Rocky 8의 `/usr/bin/python3`는 3.6이므로 Hermes 검사에는 3.11 전용 이름을 쓴다.

```bash
command -v setfacl >/dev/null
workspace=invest

sudo setfacl -m u:hermes:--x /opt/tybot /opt/tybot/subbots
sudo find /opt/tybot/subbots/hermes -type d \
  -exec setfacl -m u:hermes:rx,d:u:hermes:rx {} +
sudo find /opt/tybot/subbots/hermes -type f -exec setfacl -m u:hermes:r-- {} +
sudo find /opt/tybot/subbots/hermes -type f -perm /111 \
  -exec setfacl -m u:hermes:r-x {} +

sudo setfacl -m u:hermes:r-x /usr/bin/python3.11
sudo install -d -o root -g hermes -m 0750 /etc/hermes-pf/bin
sudo ln -sfn /usr/bin/python3.11 /etc/hermes-pf/bin/python3
sudo chown -h root:hermes /etc/hermes-pf/bin/python3

sudo setfacl -m u:hermes:--x /var/lib/tybot /var/lib/tybot/archive
sudo setfacl -m d:u:hermes:--- /var/lib/tybot/archive
sudo find /var/lib/tybot/archive -mindepth 1 -maxdepth 1 -type d \
  ! -name "$workspace" -exec setfacl -m u:hermes:---,d:u:hermes:--- {} +
sudo find "/var/lib/tybot/archive/$workspace" -type d \
  -exec setfacl -m u:hermes:rx,d:u:hermes:rx {} +
sudo find "/var/lib/tybot/archive/$workspace" -type f \
  -exec setfacl -m u:hermes:r-- {} +
if [[ -d "/var/lib/tybot/archive/$workspace/dm" ]]; then
  sudo setfacl -m u:hermes:---,d:u:hermes:--- \
    "/var/lib/tybot/archive/$workspace/dm"
fi

sudo -u hermes test -r "/var/lib/tybot/archive/$workspace"
sudo -u hermes test ! -r /var/lib/tybot/archive/mgmt
sudo -u hermes test ! -r "/var/lib/tybot/archive/$workspace/dm"
sudo -u hermes env PATH=/etc/hermes-pf/bin:/usr/bin python3 --version
```

unit은 별도로 `/var/lib/tybot/state`, shadow, QA 로그를 숨긴다.

## 4. 설치 전 검사

아직 새 Hermes를 시작하지 않는다.

TYBot 서비스 계정으로 DB의 확인된 공개 여부만 임시 위치에 내보낸 뒤, root가
Hermes의 읽기 전용 설정 디렉터리로 설치한다. Hermes에는 TYBot state 디렉터리
접근 권한을 주지 않는다.

```bash
sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/tybot.env \
  PYTHONPATH=/opt/tybot/src \
  /opt/tybot/.venv/bin/python -m tybot.archive.privacy_manifest \
  --workspace invest --out /var/lib/tybot/state/hermes-pf-privacy.json
sudo install -o root -g hermes -m 0640 \
  /var/lib/tybot/state/hermes-pf-privacy.json \
  /etc/hermes-pf/privacy-manifest.json
sudo -u hermes test -r /etc/hermes-pf/privacy-manifest.json
```

manifest 기본 유효기간은 26시간이다. 한 번만 만들면 다음 날 일반 사용자의 모든 접근이
fail-closed로 닫히므로, 12시간 갱신 타이머를 함께 설치한다. DB 조회는 `tybot`으로 하고
최종 파일 교체만 root 권한으로 수행한다.

빈 workspace와 확인된 채널 0개는 발행 실패다. 마지막 정상 manifest를 빈 파일로
덮어쓰지 않는다. Hermes 서비스는 갱신 oneshot이 끝난 뒤에만 기동한다.

```bash
sudo install -m 0644 /opt/tybot/deploy/hermes-pf-archiver.service /etc/systemd/system/
sudo install -m 0644 /opt/tybot/deploy/hermes-privacy-manifest.service /etc/systemd/system/
sudo install -m 0644 /opt/tybot/deploy/hermes-privacy-manifest.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/hermes-pf-archiver.service
sudo systemd-analyze verify /etc/systemd/system/hermes-privacy-manifest.service
sudo systemd-analyze verify /etc/systemd/system/hermes-privacy-manifest.timer
sudo systemctl start hermes-privacy-manifest.service
sudo systemctl show hermes-privacy-manifest.service \
  --property=Result --property=ExecMainStatus --no-pager
sudo systemctl enable --now hermes-privacy-manifest.timer
systemctl list-timers hermes-privacy-manifest.timer --no-pager
sudo systemctl is-enabled hermes-pf-archiver.service
```

마지막 결과는 `disabled`여야 한다. 개인정보 대조 관문은 서비스의 `ExecStartPre`에서도
다시 실행되며, 누락·미확인 채널이 있으면 기동을 거부해야 한다.

## 5. 단일 인스턴스 전환

기존 PF VM에서 먼저 실행한다.

```bash
sudo systemctl stop hermes.service
systemctl is-active hermes.service
```

결과가 `inactive`인 것을 확인한 뒤에만 TYBot 서버에서 시작한다.

```bash
sudo systemctl start hermes-pf-archiver.service
systemctl show hermes-pf-archiver.service \
  --property=NRestarts --property=ExecMainStatus \
  --property=ActiveState --property=SubState --no-pager
journalctl -u hermes-pf-archiver.service -n 100 --no-pager
```

`active/running`, `NRestarts=0`, `ExecMainStatus=0`이 아니면 진행하지 않는다.

## 6. Smoke test

순서대로 한 건씩 확인한다.

1. 공개 채널 질문이 정본 좌표와 출처를 붙여 답한다.
2. 비공개 채널 비회원 질문이 답변 생성 전에 차단된다.
3. 비공개 채널 회원 질문과 DM 질문이 허용된 채널만 읽는다.
4. 변환된 첨부의 본문을 검색하고 `read_document`로 연다.
5. `node scripts/run-digest.js daily --dry`와 `weekly --dry`가 전송 없이 완료된다.
6. `node scripts/run-ingest.js`는 종료 코드 2로 거부된다.
7. 기존 `hermes-archive-pull.timer`와 PF VM Hermes가 계속 꺼져 있다.

## 7. 사내 14일 파일럿

PF에 바로 적용하지 않는다. 동일한 빌드와 설정 형식을 사내 워크스페이스에서 14일간
먼저 검증한다. 파일럿에서도 같은 Slack 토큰을 쓰는 기존 Hermes와 새 서비스를 동시에
실행하지 않는다.

### 7.1 무발송 검증

서비스를 켜기 전에 아래 항목을 모두 확인한다.

1. privacy manifest 관문이 종료 코드 0이다.
2. 질문, DM, 일간 및 주간 요약을 dry-run으로 실행해 출처 좌표가 Archiver 정본을 가리킨다.
3. 공개 채널, 비공개 채널 회원, 비회원의 결과가 각각 기대한 권한 경계를 지킨다.
4. 첨부 신규 생성, 재변환, 삭제가 한 프로세스의 캐시에 즉시 반영된다.
5. 모든 원문 쓰기 진입점이 `HERMES_MODE=pf-archiver`에서 종료 코드 2로 거부된다.

### 7.2 제한 운영

첫날은 사내 지정 채널과 지정 사용자 DM만 허용한다. 기존 Hermes를 멈춘 것을 확인한 뒤
새 서비스를 시작한다. 제한 대상을 넓히는 것은 하루 단위로 하며, 권한 오류나 중복 발송이
한 건이라도 있으면 즉시 롤백한다.

매일 다음을 기록한다.

- 서비스 재시작 수와 마지막 종료 상태
- privacy manifest 생성 시각과 관문 결과
- 권한 거부, manifest 불일치, 정본 읽기 실패 건수
- 질문과 DM 표본의 출처 `locator` 및 `message_ts` 대조 결과
- 일간/주간 요약의 중복 발송 및 누락 건수
- 첨부 신규 생성, 재변환, 삭제 후 검색 반영 여부
- Hermes 원문 쓰기 거부 로그와 Archiver 정본 쓰기 시도 0건

```bash
systemctl show hermes-pf-archiver.service \
  --property=NRestarts --property=ExecMainStatus \
  --property=ActiveState --property=SubState --no-pager
journalctl -u hermes-pf-archiver.service --since '24 hours ago' --no-pager
```

### 7.3 PF 승격 조건

다음 조건을 14일 연속 만족한 뒤에만 PF 전환 일정을 잡는다.

- 권한 밖 채널 노출 0건
- 원문 쓰기 및 Git 아카이브 쓰기 0건
- 중복 질문 응답과 중복 검토 DM 0건
- manifest 누락, 만료, 중복 ID를 관문이 모두 거부
- 공개에서 비공개로 바뀐 채널과 새 비공개 채널이 재시작 없이 닫힘
- 첨부 캐시 신규, 재변환, 삭제 회귀 0건
- 질문, DM, 일간 및 주간 요약의 출처 좌표 오류 0건
- 운영자가 롤백 절차를 한 번 실제로 수행하고 정상 복구 확인

PF에는 검증된 커밋과 설정 형식, manifest 갱신 절차만 전달한다. 사내 토큰, manifest,
아카이브 및 로그는 전달하지 않는다. PF 환경에서도 시작 직전에 같은 privacy 관문과
무발송 검증을 다시 실행한다.

## 8. 롤백

TYBot 서버에서 먼저 중지한다.

```bash
sudo systemctl stop hermes-pf-archiver.service
sudo systemctl disable --now hermes-privacy-manifest.timer
systemctl is-active hermes-pf-archiver.service
```

`inactive` 확인 뒤 PF VM에서 기존 Hermes를 시작한다. 두 인스턴스를 동시에 켜서 확인하지
않는다. 롤백은 기존 PF `HERMES_MODE=pf`와 기존 Git 아카이브를 그대로 사용한다.

## 운영 전 남은 조건

- 채널 공개 여부 manifest에서 누락·미확인 채널이 0개여야 한다.
- 전체 Python·Hermes 시험이 현재 커밋에서 통과해야 한다.
- 실제 TYBot 서버의 Node 버전이 Hermes의 `node >= 20` 조건을 만족해야 한다.
- `setfacl` 패키지와 재부팅 뒤 ACL 상속을 실제 새 파일로 확인해야 한다.
