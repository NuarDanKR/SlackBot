# 한 서버에서 Hermes Archiver 인스턴스 운영

상태: **사내 파일럿 준비**. 먼저 `tyit` 인스턴스를 14일 검증하고, 통과한 뒤 같은
서버에 PF 인스턴스를 추가한다. 두 인스턴스는 같은 코드를 사용하지만 계정, Slack 토큰,
설정, 상태, manifest와 읽을 수 있는 Archiver workspace가 모두 다르다.

## 원칙

- 둘 다 `HERMES_MODE=pf-archiver`로 실행하며 원문을 쓰지 않는다.
- 사내는 `HERMES_DOMAIN=enterprise`, PF는 `HERMES_DOMAIN=pf-construction`을 쓴다.
- 코드만 공유한다. `/var/lib/hermes/<instance>`와 `/etc/hermes/<instance>`는 공유하지 않는다.
- OS 계정도 `hermes-<instance>`로 나눈다. 한 계정에 두 workspace ACL을 주지 않는다.
- instance는 Archiver workspace key와 같아야 하며 `[a-z][a-z0-9]{0,15}`만 허용한다.
- 같은 Slack App/Bot 토큰으로 기존 Hermes와 새 서비스를 동시에 실행하지 않는다.
- 인터넷 인바운드 포트는 열지 않는다. Slack Socket Mode 아웃바운드만 사용한다.

## 1. 템플릿 설치

배포가 아래 세 템플릿을 설치한다. 설정되지 않은 인스턴스를 자동으로 켜지는 않는다.

```bash
sudo systemctl daemon-reload
sudo systemd-analyze verify \
  /etc/systemd/system/hermes-archiver@.service \
  /etc/systemd/system/hermes-privacy-manifest@.service \
  /etc/systemd/system/hermes-privacy-manifest@.timer
```

## 2. 사내 인스턴스 디렉터리와 계정

첫 파일럿의 instance와 Archiver workspace는 모두 `tyit`이다.

```bash
instance=tyit
workspace=tyit
account=hermes-tyit

getent group hermes-runtime >/dev/null || sudo groupadd --system hermes-runtime
getent group "$account" >/dev/null || sudo groupadd --system "$account"
id -u "$account" >/dev/null 2>&1 || sudo useradd --system \
  --gid "$account" --groups hermes-runtime \
  --home-dir "/var/lib/hermes/$instance" --shell /sbin/nologin "$account"

sudo install -d -o root -g "$account" -m 0750 "/etc/hermes/$instance"
sudo install -d -o "$account" -g "$account" -m 0700 "/var/lib/hermes/$instance"
sudo install -d -o "$account" -g "$account" -m 0700 "/var/lib/hermes/$instance/state"
sudo install -d -o root -g "$account" -m 0750 "/etc/hermes/$instance/bin"
sudo ln -sfn /usr/bin/python3.11 "/etc/hermes/$instance/bin/python3"
sudo chown -h root:"$account" "/etc/hermes/$instance/bin/python3"
sudo systemd-tmpfiles --create /etc/tmpfiles.d/hermes-bot-locks.conf
```

기존 설정을 `/var/lib/hermes/tyit/config.json`으로 옮기고 다음을 확인한다.

- `domain`은 `enterprise`다.
- `archiver.root`는 `/var/lib/tybot/archive`, `archiver.workspace`는 `tyit`이다.
- `archiver.privacyManifest`는 `/etc/hermes/tyit/privacy-manifest.json`이다.
- `privateChannels`와 질문·DM·일간/주간 요약 설정은 사내 값이다.
- `digest.ingest.enabled`와 `digest.ingestPre.enabled`는 꺼져 있다.

## 3. 사내 환경 파일

```bash
sudo install -o root -g hermes-tyit -m 0640 /dev/null /etc/hermes/tyit/hermes.env
sudoedit /etc/hermes/tyit/hermes.env
sudo install -o root -g hermes-tyit -m 0640 /dev/null /etc/hermes/tyit/privacy.env
sudoedit /etc/hermes/tyit/privacy.env
```

`hermes.env`에는 저장소 밖의 실제 값만 둔다. 인라인 주석은 쓰지 않는다.

```dotenv
SLACK_BOT_TOKEN=<secret>
SLACK_APP_TOKEN=<secret>
ANTHROPIC_API_KEY=<secret>
HERMES_ARCHIVER_WORKSPACE=tyit
HERMES_DOMAIN=enterprise
HERMES_SLACK_TEAM_ID=<auth.test team_id>
HERMES_SLACK_BOT_USER_ID=<auth.test user_id>
```

`privacy.env`:

```dotenv
HERMES_PRIVACY_WORKSPACE=tyit
```

`HERMES_MODE`, data/state root, Archiver root와 manifest 경로는 unit이 고정한다.

## 4. 코드와 Archiver ACL

공유 코드에는 `hermes-runtime` 그룹의 읽기 권한만 준다. 각 인스턴스 계정에는 자기
workspace만 읽게 하고, DM과 다른 workspace는 명시적으로 막는다.

```bash
sudo setfacl -m g:hermes-runtime:--x /opt/tybot /opt/tybot/subbots
sudo find /opt/tybot/subbots/hermes -type d \
  -exec setfacl -m g:hermes-runtime:rx,d:g:hermes-runtime:rx {} +
sudo find /opt/tybot/subbots/hermes -type f \
  -exec setfacl -m g:hermes-runtime:r-- {} +
sudo find /opt/tybot/subbots/hermes -type f -perm /111 \
  -exec setfacl -m g:hermes-runtime:r-x {} +
sudo setfacl -m g:hermes-runtime:r-x /usr/bin/python3.11

sudo setfacl -m u:hermes-tyit:--x /var/lib/tybot /var/lib/tybot/archive
sudo setfacl -m d:u:hermes-tyit:--- /var/lib/tybot/archive
sudo find /var/lib/tybot/archive -mindepth 1 -maxdepth 1 -type d \
  ! -name tyit -exec setfacl -m u:hermes-tyit:---,d:u:hermes-tyit:--- {} +
sudo find /var/lib/tybot/archive/tyit -type d \
  -exec setfacl -m u:hermes-tyit:rx,d:u:hermes-tyit:rx {} +
sudo find /var/lib/tybot/archive/tyit -type f \
  -exec setfacl -m u:hermes-tyit:r-- {} +
sudo install -d -o tybot -g tybot -m 0750 /var/lib/tybot/archive/tyit/dm
sudo setfacl -m u:hermes-tyit:---,d:u:hermes-tyit:--- /var/lib/tybot/archive/tyit/dm

sudo -u hermes-tyit test -r /var/lib/tybot/archive/tyit
sudo -u hermes-tyit test ! -r /var/lib/tybot/archive/mgmt
sudo -u hermes-tyit test ! -r /var/lib/tybot/archive/tyit/dm
sudo -u hermes-tyit env PATH=/etc/hermes/tyit/bin:/usr/bin python3 --version
```

unit도 mount namespace에서 다른 workspace 전체와 `dm/`을 가린다. ACL은 그 앞의 첫
방어선이다. 배포가 inode를 교체할 수 있으므로 **배포할 때마다 4절의 코드·workspace
ACL을 재적용**하고 `sudo -u hermes-tyit test -r`/`test ! -r`를 다시 확인한다.

## 5. Manifest와 준비 점검

```bash
sudo systemctl start hermes-privacy-manifest@tyit.service
systemctl show hermes-privacy-manifest@tyit.service \
  --property=Result --property=ExecMainStatus --no-pager
sudo -u hermes-tyit test -r /etc/hermes/tyit/privacy-manifest.json

cd /opt/tybot/subbots/hermes
sudo -u hermes-tyit /usr/bin/bash -c '
  set -a
  source /etc/hermes/tyit/hermes.env
  set +a
  export HERMES_MODE=pf-archiver
  export HERMES_DATA_ROOT=/var/lib/hermes/tyit
  export HERMES_STATE_DIR=/var/lib/hermes/tyit/state
  export HERMES_ARCHIVER_ROOT=/var/lib/tybot/archive
  export HERMES_PRIVACY_MANIFEST=/etc/hermes/tyit/privacy-manifest.json
  export PATH=/etc/hermes/tyit/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin
  /usr/bin/npm run check:archive
'
```

점검은 0으로 끝나야 한다. 수집 명령은 반대로 차단되어야 한다.

```bash
sudo -u hermes-tyit env \
  HERMES_MODE=pf-archiver HERMES_DOMAIN=enterprise \
  HERMES_DATA_ROOT=/var/lib/hermes/tyit \
  HERMES_STATE_DIR=/var/lib/hermes/tyit/state \
  HERMES_ARCHIVER_ROOT=/var/lib/tybot/archive \
  HERMES_ARCHIVER_WORKSPACE=tyit \
  HERMES_PRIVACY_MANIFEST=/etc/hermes/tyit/privacy-manifest.json \
  /usr/bin/node scripts/run-ingest.js
test $? -eq 2
```

## 6. 사내 인스턴스 시작

같은 Slack 토큰을 쓰는 기존 프로세스를 먼저 멈춘다. 새 서비스가 정상임을 확인하기 전에는
기존 설정이나 데이터를 지우지 않는다.

```bash
sudo systemctl stop hermes.service
systemctl is-active hermes.service

sudo systemctl enable --now hermes-privacy-manifest@tyit.timer
sudo systemctl start hermes-archiver@tyit.service
systemctl show hermes-archiver@tyit.service \
  --property=NRestarts --property=ExecMainStatus \
  --property=ActiveState --property=SubState --no-pager
journalctl -u hermes-archiver@tyit.service -n 100 --no-pager
```

기대값은 `active/running`, `NRestarts=0`, `ExecMainStatus=0`이다.
누락된 설정은 `ConditionPathExists`로 조용히 건너뛰지 않고 `ExecStartPre` 실패로 남는다.
같은 `HERMES_SLACK_BOT_USER_ID`를 가진 다른 Hermes가 돌고 있으면 공용 flock이 기동을
거부한다. `auth.test`의 team/user가 환경 파일의 기대값과 다르면 Socket Mode를 열기 전에
종료한다.

## 7. 사내 14일 파일럿

아래 조건이 14일 연속 만족된 뒤에만 PF 인스턴스를 추가한다.

- 권한 밖 채널 노출 0건
- 원문 쓰기와 Git 아카이브 쓰기 0건
- 중복 질문 답변과 중복 검토 DM 0건
- manifest 만료, workspace 불일치, 중복 ID를 관문이 모두 거부
- 질문·DM·일간/주간 요약의 출처 좌표 오류 0건
- 첨부 신규·재변환·삭제가 재시작 없이 반영
- 서비스 재시작과 rollback을 각각 한 번 실측

매일 다음을 기록한다.

```bash
systemctl show hermes-archiver@tyit.service \
  --property=NRestarts --property=ExecMainStatus \
  --property=ActiveState --property=SubState --no-pager
systemctl list-timers hermes-privacy-manifest@tyit.timer --no-pager
journalctl -u hermes-archiver@tyit.service --since '24 hours ago' --no-pager
```

## 8. PF 인스턴스 추가

PF는 별도 서버가 아니라 같은 서버의 두 번째 인스턴스다. 아래 값으로 2~6절을 반복한다.

```text
instance=invest
account=hermes-invest
workspace=invest
HERMES_DOMAIN=pf-construction
data=/var/lib/hermes/invest
state=/var/lib/hermes/invest/state
config=/etc/hermes/invest
service=hermes-archiver@invest.service
timer=hermes-privacy-manifest@invest.timer
```

`/var/lib/hermes/invest/config.json`, `/etc/hermes/invest/hermes.env`, PF 전용 Slack 토큰과
`invest` workspace ACL을 별도로 만든다. `hermes-invest` 계정에 `tyit` ACL을 주지 않고,
`hermes-tyit` 계정에 `invest` ACL을 주지 않는다. 시작 전 다음이 모두 성공해야 한다.

```bash
sudo -u hermes-invest test -r /var/lib/tybot/archive/invest
sudo -u hermes-invest test ! -r /var/lib/tybot/archive/tyit
sudo -u hermes-tyit test ! -r /var/lib/tybot/archive/invest
sudo -u hermes-invest test ! -r /var/lib/tybot/archive/invest/dm
```

## 9. 롤백

사내 인스턴스만 중단할 때:

```bash
sudo systemctl stop hermes-archiver@tyit.service
sudo systemctl disable --now hermes-privacy-manifest@tyit.timer
systemctl is-active hermes-archiver@tyit.service
```

PF는 `@invest`만 중단한다. 한 인스턴스의 롤백에서 다른 인스턴스의 서비스, 상태,
manifest, ACL을 변경하지 않는다. 기존 Hermes를 되살릴 때도 같은 Slack 토큰의 새
인스턴스가 `inactive`인지 먼저 확인한다.
