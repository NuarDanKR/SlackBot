# 인프라·보안 담당자 요청서 — 조직/인사 스냅샷 전송 경로

_TYBot 그룹웨어 연동 · 방식 B(내부망 → DMZ 파일 푸시) 구성_
_배경 설계: [oracle-sync.md](../design/oracle-sync.md)_

---

## 0. 한 줄 요청

> **내부망 배치서버 1대**에서 **DMZ 봇서버 1대**로 **SFTP(22/tcp) 단방향** 접속 1건을 허용해 주세요.
> 그 외 신규 개방은 없습니다. DMZ 에서 내부망으로 들어가는 규칙은 요청하지 않습니다.

### 먼저 정정 — "인바운드 0개"에 대한 보정
그동안 이 시스템을 "인바운드 포트 0개"로 설명했습니다. 정확히는 **인터넷에서 들어오는 인바운드가
0개**라는 뜻입니다(Slack 연동은 봇이 밖으로 나가는 WebSocket 이라 그렇습니다).

이 요청은 **내부망에서 DMZ 로 들어오는** 규칙 1건을 추가합니다. 성질이 다릅니다:
- 출발지가 인터넷이 아니라 사내 특정 서버 1대
- 프로토콜은 SSH(SFTP), 인증은 키 기반
- 방향이 안쪽→바깥쪽이라 **DMZ 가 내부망을 향해 세션을 열지 않습니다**

대안(방식 A)은 DMZ→내부망 Oracle 1521 개방입니다. 그쪽이 위험이 더 큽니다(3절 비교).

---

## 1. 방화벽 규칙 (요청 사항 전부)

| 항목 | 값 |
|---|---|
| 출발지 | `<내부망 배치서버 IP>` (단일 호스트) |
| 목적지 | `<DMZ 봇서버 IP>` (단일 호스트, hostname `tyai`) |
| 포트/프로토콜 | **22/tcp** |
| 방향 | 내부망 → DMZ **단방향** |
| 용도 | 조직도·인사기본 스냅샷 파일(JSONL) 전송 |
| 주기 | 야간 1회(예: 03:20) + 필요 시 수동 |
| 전송량 | 파일 2~3개, 합계 수 MB 이하 |
| 계정 | `tybot_ingest` — SFTP 전용, 셸 없음, 쓰기 경로 1개 |

**요청하지 않는 것** (오해 방지용으로 명시):
- DMZ → 내부망 어떤 포트도 아님
- 인터넷 → DMZ 어떤 포트도 아님
- DMZ 서버의 기존 관리 접속 경로 변경 없음

---

## 2. 담당 분담

| 작업 | 담당 | 절 |
|---|---|---|
| 방화벽 규칙 1건 등록 | 인프라·보안 | 1절 |
| DMZ 서버에 SFTP 전용 계정·디렉터리 생성 | 서버 관리자 (또는 우리) | 4절 |
| SSH 키쌍 생성 및 공개키 전달 | 내부망 배치 담당 | 5절 |
| Oracle 뷰·읽기전용 계정 | DBA(우리) | [oracle-sync.md](../design/oracle-sync.md) 1절 |
| 스냅샷 추출 스크립트 | DBA(우리) + 배치 담당 | 6절 |
| 봇 서버 반영 잡 | 우리 | 7절 |

---

## 3. 왜 이 방향인지 (심사 질문 대비)

| 질문 | 답 |
|---|---|
| 왜 DB 직결(방식 A)이 아닌가 | DMZ→내부망 1521 개방이 필요하고, **DMZ 서버에 Oracle 계정 정보가 상주**합니다. DMZ 침해 시 그 자격증명으로 내부 DB 세션이 열립니다. 방식 B 는 DMZ 에 DB 자격증명이 존재하지 않습니다 |
| DMZ 가 침해되면 스냅샷은? | 조직도·인사기본(사번·이름·이메일·소속)만 노출됩니다. 주민번호·연락처·급여는 뷰에서 제외합니다 |
| 왜 인터넷 경유(GitHub 등)를 안 쓰나 | 개인정보를 외부 클라우드로 보내지 않습니다. 사내 구간에서 끝냅니다 |
| SSH 대신 사내 파일전송 시스템을 쓰면? | **가능하면 그게 더 좋습니다.** 이미 승인된 전송 경로가 있으면 신규 규칙이 필요 없습니다 → 8절 |
| 상시 연결인가 | 아닙니다. 야간 1회 수 초~수십 초. 나머지 시간엔 세션이 없습니다 |
| 로그는 남는가 | DMZ `/var/log/secure` 에 SFTP 접속·전송 기록, 봇 서버에 반영 이력(`sync_run` 테이블) |

---

## 4. DMZ 봇서버 준비 (서버 관리자 작업)

SFTP 만 되고 셸은 안 되는 계정을 만듭니다. 업로드 경로도 하나로 제한합니다.

```bash
# 4-1. 전용 계정 (로그인 셸 없음)
sudo groupadd -f sftponly
sudo useradd -m -g sftponly -s /sbin/nologin tybot_ingest

# 4-2. chroot 루트는 root 소유여야 한다 (OpenSSH 요구사항)
sudo mkdir -p /var/sftp/tybot_ingest/inbox
sudo chown root:root /var/sftp/tybot_ingest
sudo chmod 755      /var/sftp/tybot_ingest
sudo chown tybot_ingest:sftponly /var/sftp/tybot_ingest/inbox
sudo chmod 750      /var/sftp/tybot_ingest/inbox

# 4-3. 공개키 등록 (5절에서 받은 값)
sudo mkdir -p /var/sftp/tybot_ingest/.ssh
sudo tee /var/sftp/tybot_ingest/.ssh/authorized_keys >/dev/null <<'EOF'
<내부망 배치서버 공개키 한 줄>
EOF
sudo chown -R tybot_ingest:sftponly /var/sftp/tybot_ingest/.ssh
sudo chmod 700 /var/sftp/tybot_ingest/.ssh
sudo chmod 600 /var/sftp/tybot_ingest/.ssh/authorized_keys
```

```bash
# 4-4. sshd 설정 — 이 계정만 SFTP 로 묶는다
sudo tee /etc/ssh/sshd_config.d/50-tybot-ingest.conf >/dev/null <<'EOF'
Match User tybot_ingest
    ChrootDirectory /var/sftp/tybot_ingest
    ForceCommand internal-sftp -d /inbox
    AllowTcpForwarding no
    X11Forwarding no
    PermitTunnel no
    GatewayPorts no
    PasswordAuthentication no
    AuthenticationMethods publickey
EOF

sudo sshd -t && sudo systemctl reload sshd    # -t 로 문법 검사 후 reload
```

> `sshd -t` 가 실패하면 reload 하지 마세요. 설정 오류로 관리 접속이 끊길 수 있습니다.

```bash
# 4-5. 방화벽 — 그 내부망 IP 에서만 22 허용
sudo firewall-cmd --permanent --new-zone=internal-sync 2>/dev/null || true
sudo firewall-cmd --permanent --zone=internal-sync --add-source=<내부망 배치서버 IP>/32
sudo firewall-cmd --permanent --zone=internal-sync --add-port=22/tcp
sudo firewall-cmd --reload
sudo firewall-cmd --zone=internal-sync --list-all
```

```bash
# 4-6. SELinux (Rocky 8 은 enforcing 유지)
sudo semanage fcontext -a -t ssh_home_t "/var/sftp/tybot_ingest/\.ssh(/.*)?"
sudo restorecon -Rv /var/sftp/tybot_ingest
```

```bash
# 4-7. 봇이 읽어갈 위치로 연결 (봇은 /var/lib/tybot 만 쓴다)
sudo mkdir -p /var/lib/tybot/inbox
sudo mount --bind /var/sftp/tybot_ingest/inbox /var/lib/tybot/inbox
echo '/var/sftp/tybot_ingest/inbox /var/lib/tybot/inbox none bind 0 0' | sudo tee -a /etc/fstab
sudo setfacl -m u:tybot:rx /var/sftp/tybot_ingest/inbox     # 봇 계정 읽기 권한
```
(심볼릭 링크는 chroot 안에서 동작하지 않으므로 bind mount 를 씁니다.)

---

## 5. SSH 키 (내부망 배치 담당 작업)

```bash
# 내부망 배치서버에서 실행. 개인키는 이 서버를 벗어나지 않는다.
ssh-keygen -t ed25519 -f /etc/tybot/sync_key -N "" -C "tybot-snapshot-push"
chmod 600 /etc/tybot/sync_key
cat /etc/tybot/sync_key.pub        # ← 이 한 줄을 서버 관리자에게 전달 (4-3)
```
- **개인키를 메일·메신저로 보내지 마세요.** 공개키만 전달합니다.
- 이 키는 SFTP 업로드 외에는 아무것도 못 합니다(4-4 의 `ForceCommand`).

접속 확인:
```bash
sftp -i /etc/tybot/sync_key tybot_ingest@<DMZ 봇서버 IP>
# 접속 후 pwd → /inbox 만 보이고, cd / 해도 chroot 밖으로 못 나가야 정상
```

---

## 6. 스냅샷 추출·전송 (내부망 배치, DBA + 배치 담당)

```bash
#!/usr/bin/env bash
# /etc/tybot/push_snapshot.sh — 야간 1회 cron
set -euo pipefail
OUT=/var/tmp/tybot
DMZ=<DMZ 봇서버 IP>
mkdir -p "$OUT"

# 6-1. 뷰 → JSONL (컬럼은 V_TYBOT_* 뷰에 이미 제한돼 있다)
sqlplus -s TYBOT_RO/"$ORA_PW"@ORCL @/etc/tybot/export_org.sql > "$OUT/org.jsonl"
sqlplus -s TYBOT_RO/"$ORA_PW"@ORCL @/etc/tybot/export_emp.sql > "$OUT/emp.jsonl"

# 6-2. 빈 파일·비정상 축소 방어 (조회 실패를 '전원 퇴직'으로 넘기지 않는다)
[[ $(wc -l < "$OUT/org.jsonl") -ge 10 ]] || { echo "조직 스냅샷 비정상"; exit 1; }
[[ $(wc -l < "$OUT/emp.jsonl") -ge 50 ]] || { echo "인사 스냅샷 비정상"; exit 1; }

# 6-3. 체크섬 — 전송 중 손상·부분 업로드 검출
( cd "$OUT" && sha256sum org.jsonl emp.jsonl > snapshot.sha256 )

# 6-4. 업로드. 임시 이름으로 올린 뒤 rename → 봇이 반쪽 파일을 읽지 않게
sftp -i /etc/tybot/sync_key -o StrictHostKeyChecking=yes tybot_ingest@"$DMZ" <<EOF
put $OUT/org.jsonl      org.jsonl.part
put $OUT/emp.jsonl      emp.jsonl.part
put $OUT/snapshot.sha256 snapshot.sha256.part
rename org.jsonl.part       org.jsonl
rename emp.jsonl.part       emp.jsonl
rename snapshot.sha256.part snapshot.sha256
EOF
```

- `StrictHostKeyChecking=yes` + 사전 `ssh-keyscan` 등록 → 중간자 대비.
- 임시 이름 후 rename 이 중요합니다. 봇이 업로드 중인 파일을 읽으면 부분 반영이 됩니다.
- Oracle 비밀번호는 스크립트에 넣지 말고 별도 파일(0600) 또는 wallet 을 씁니다.

---

## 7. 봇 서버 반영 (우리 작업)

`tybot-sync.timer` 가 `/var/lib/tybot/inbox` 를 확인해:
1. `snapshot.sha256` 검증 → 실패면 반영하지 않고 경고
2. 기존 행 수의 90% 미만이면 중단(조회 실패 방어)
3. 스테이징 후 트랜잭션 1개로 교체, `sync_run` 에 이력 기록
4. 반영 완료 파일은 `inbox/processed/<날짜>/` 로 이동

상세는 [oracle-sync.md](../design/oracle-sync.md) 4·6절.

---

## 8. 신규 방화벽 규칙 없이 가는 대안

보안팀이 22/tcp 개방을 거절하거나 절차가 오래 걸릴 때.

| 대안 | 방법 | 판단 |
|---|---|---|
| **사내 파일전송 시스템** | 이미 승인된 전송 경로로 스냅샷을 DMZ 에 떨어뜨린다 | **1순위.** 신규 규칙 0건 |
| 공유 스토리지(NAS/오브젝트) | 양쪽이 이미 접근 가능한 저장소를 경유 | 좋음. 접근 권한은 파일 단위로 제한 |
| 수동 반입 | 주 1회 담당자가 파일을 올린다 | 초기 검증엔 충분. 조직 정보는 하루 단위로 바뀌므로 운영은 부적합 |
| DMZ→내부망 pull(방식 A) | 1521 개방 + DMZ 에 DB 계정 | **최후 수단.** 위험이 가장 큼 |

첫 두 대안이 가능하면 1절 요청은 취소하고 그 경로를 씁니다. **먼저 "이미 있는 전송 경로가
있는지" 물어보는 게 가장 빠릅니다.**

---

## 9. 수용 검증 (구성 후 함께 확인)

| # | 확인 | 기대 |
|---|---|---|
| 1 | 내부망 배치서버에서 `sftp -i ... tybot_ingest@DMZ` | 접속 성공, `pwd` = `/inbox` |
| 2 | 같은 세션에서 `cd /` 후 `ls` | chroot 밖이 보이지 않음 |
| 3 | 같은 세션에서 `!bash` 또는 `ssh tybot_ingest@DMZ` | **셸 접속 거부** |
| 4 | 다른 내부망 IP 에서 `ssh tybot_ingest@DMZ` | 연결 차단(방화벽) |
| 5 | DMZ 에서 `ssh <내부망 Oracle IP>` / `nc -zv <Oracle IP> 1521` | **차단**(역방향 없음 확인) |
| 6 | 파일 업로드 후 DMZ `sudo -u tybot ls /var/lib/tybot/inbox` | 파일 보임 |
| 7 | DMZ `sudo -u tybot_ingest ls /var/lib/tybot` | 권한 없음(업로드 경로 외 접근 불가) |
| 8 | `sudo journalctl -u sshd \| grep tybot_ingest` | 접속·전송 기록 남음 |

3·5번이 이 구성의 핵심입니다 — 계정이 셸을 못 얻고, DMZ 가 내부망을 향해 나가지 못합니다.

---

## 10. 폐기 절차 (연동 중단 시)

```bash
sudo firewall-cmd --permanent --delete-zone=internal-sync && sudo firewall-cmd --reload
sudo rm /etc/ssh/sshd_config.d/50-tybot-ingest.conf && sudo sshd -t && sudo systemctl reload sshd
sudo userdel -r tybot_ingest
sudo umount /var/lib/tybot/inbox   # /etc/fstab 항목도 제거
```
내부망 쪽에서는 `/etc/tybot/sync_key*` 삭제와 cron 해제.
