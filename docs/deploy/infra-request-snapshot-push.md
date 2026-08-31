# 인프라 요청 — 봇서버에서 그룹웨어 Oracle 조회

_TYSlack 그룹웨어 연동 · 2026-08-31 **방식 A(직접 조회)로 확정**_
_배경 설계: [oracle-sync.md](../design/oracle-sync.md)_

## 요청 — 방화벽 규칙 1건

```
출발지: <DMZ 봇서버 IP>   목적지: 172.16.10.20   포트: 1523/tcp
```

DMZ 기준 아웃바운드, DB 기준 인바운드입니다. 방화벽이 상태를 추적하므로 **응답
트래픽용 반대 방향 규칙은 필요 없습니다.** 사내에 같은 방식으로 조회하는 서버가 이미
있어 그 선례를 따릅니다.

> **이전 요청(내부망 배치서버 → DMZ SFTP 22/tcp)은 취소되었습니다.**
> 배치서버·SFTP 계정·chroot·bind mount 가 전부 필요 없어졌습니다.
> 참고로 그 요청도 Oracle **DB 서버**에서 파일을 보내는 구성이 아니었습니다.
> Oracle 에 접속만 하는 별개 서버를 두는 방식이었고, DB 서버에 외부로 나가는 키를
> 두는 구성은 설계 단계에서 배제했습니다.

---

## 1. 왜 필요한가

봇은 **누가 무엇을 볼 수 있는지**를 조직도로 판단합니다. 소속을 모르면 둘 중 하나가
됩니다 — 권한을 넓게 열어 **볼 수 없어야 할 자료가 보이거나**, 아무것도 못 보여줘
봇이 쓸모없어집니다. 그룹웨어에 정확한 조직도가 있으니 하루 1회 복제합니다.

여기에 팀 일정 알림(시작 30분/10분 전 Slack 공지)이 붙습니다.

---

## 2. 노출 범위 — 심사 질문 대비

봇서버가 침해되면 이 계정으로 무엇을 할 수 있는지가 핵심입니다.
**Oracle 쪽에서 이미 좁혀 놓고 확인까지 마쳤습니다(2026-08-31).**

| | 상태 |
|---|---|
| 계정 권한 | `CREATE SESSION` + 읽기 전용 뷰 4개 `SELECT`. 그 외 없음 |
| 롤 | 없음 (`CONNECT`·`RESOURCE` 도 주지 않음) |
| 원본 테이블 | **조회 불가** — 봇 계정으로 실행해 `ORA-00942` 확인 |
| 테이블스페이스 할당량 | 0 — 테이블을 만들 수 없음 |
| 쓰기 | 불가 |

읽는 값: 조직코드·조직명·상위조직 / 사번·이름·회사이메일·소속·직무 /
부서 권한이 있는 팀 일정의 제목·장소·시각.

**읽지 못하는 값**: 로그인 비밀번호(`LOGONPASSWORD`)·휴대폰·전화·주소·생년월일·
일정 설명 본문·참석자·첨부. 뷰에 없고 원본 테이블 권한도 없습니다.

| 질문 | 답 |
|---|---|
| 상시 연결인가 | 아닙니다. 일정 조회가 1분마다 수백 ms, 조직 동기화는 야간 1회 |
| 부하는 | 일정은 인덱스 범위 스캔에 결과 수십 행. 조직은 야간 1회 1,370+1,129행 |
| 자격증명 보관 | `/etc/tybot/oracle.env` (root 소유 0600), systemd `EnvironmentFile` 주입 |
| 이력은 | 봇서버 PostgreSQL 의 `sync_run` · `schedule_sync_run` |

### 선택 — 접속 출발지를 리스너에서 한 겹 더 막기

방화벽만으로도 출발지가 1대로 묶이지만, DB 서버 `sqlnet.ora` 에서
`TCP.VALIDNODE_CHECKING = YES` / `TCP.INVITED_NODES = (...)` 를 쓰면 자격증명이
유출돼도 다른 곳에서는 접속 자체가 안 됩니다.

**다만 이 설정은 리스너 전체에 적용됩니다.** `TCP.INVITED_NODES` 에 기존 클라이언트가
하나라도 빠지면 그쪽이 즉시 끊깁니다. 이미 켜져 있으면 봇서버 IP만 추가하면 되고,
새로 켜는 것이면 위험 대비 이득을 판단해 주십시오. **켜지 않아도 연동은 동작합니다.**

---

## 3. Oracle 쪽 상태 — 추가 요청 없음

| | 상태 |
|---|---|
| `TYSLACK` 스키마 (뷰 소유자) | 완료 |
| `V_TYSLACK_ORG` · `V_TYSLACK_EMP` | 완료 · 조직 1,370 / 인사 1,129행 반영 확인 |
| `V_TYSLACK_SCHEDULE_FOLDER` · `V_TYSLACK_SCHEDULE` | 완료 · 폴더 269개 조회 확인 |
| `TYSLACK_BOT` 계정 + 뷰 4개 GRANT | 완료 · 원본 차단 확인 |

계정 만료만 확인해 주십시오 — 12c 기본 프로파일은 `PASSWORD_LIFE_TIME` 이 180일이라
그대로 두면 **반년 뒤 봇이 조용히 끊깁니다.**

```sql
SELECT resource_name, limit FROM dba_profiles
 WHERE profile = 'DEFAULT'
   AND resource_name IN ('PASSWORD_LIFE_TIME', 'FAILED_LOGIN_ATTEMPTS');
```

---

## 4. 봇서버 설정 (우리 작업)

```bash
# 4-1. 접속 정보. root 소유 0600.
sudo install -d -m 750 /etc/tybot
sudo tee /etc/tybot/oracle.env >/dev/null <<'EOF'
ORACLE_HOST=172.16.10.20
ORACLE_PORT=1523
ORACLE_SERVICE=BPROD
ORACLE_SCHEMA=TYSLACK
ORACLE_USER=TYSLACK_BOT
ORACLE_PASSWORD=<전달받은 값>
EOF
sudo chown root:root /etc/tybot/oracle.env
sudo chmod 600       /etc/tybot/oracle.env

# 4-2. Instant Client 는 필요 없다 — python-oracledb thin 모드가 12.1 을 지원한다.
pip install oracledb

# 4-3. 조직·인사 — 야간 1회
python scripts/oracle_export.py --out /var/lib/tybot/snapshots
python -m tybot.orgsync

# 4-4. 일정 — live 1분, reconcile 매시간
python scripts/schedule_export.py --out /var/lib/tybot/schedule --mode live --horizon-hours 48
python scripts/schedule_export.py --out /var/lib/tybot/schedule --mode reconcile --horizon-days 30
```

**추출 → 파일 → 반영 두 단계를 유지합니다.** 직접 조회니 한 번에 밀어 넣을 수도
있지만, 그러면 조회가 반쯤 실패한 결과를 검사 없이 반영하게 됩니다. 파일 사이에
체크섬·행수 급감·순환 참조 검사가 들어갑니다. 조직 반영에서 이 검사가 실제로 이름 없는
3건을 잡아 1,120명 전량 롤백을 막았습니다.

`systemd` 타이머로 돌리고 **봇 프로세스와 분리**합니다.

---

## 5. 수용 검증 (구성 후 함께 확인)

| # | 확인 | 기대 |
|---|---|---|
| 1 | 봇서버에서 `nc -zv 172.16.10.20 1523` | 열림 |
| 2 | 다른 DMZ 서버에서 같은 명령 | **차단** |
| 3 | 봇서버에서 `python scripts/oracle_export.py --out /tmp/t` | 조직 1,370 · 인사 1,129행 |
| 4 | 봇서버에서 `SELECT count(*) FROM COVI_SMART4J.SYS_OBJECT_USER` | **ORA-00942** |
| 5 | 같은 계정으로 `INSERT`/`UPDATE` 시도 | **권한 없음** |
| 6 | 봇서버에서 Oracle 외 내부망 IP 로 `nc -zv` | **차단** (구멍이 1개인지 확인) |
| 7 | `ls -l /etc/tybot/oracle.env` | `-rw------- root root` |

**4·5·6번이 핵심입니다** — 계정이 원본을 못 읽고, 쓰지 못하고, 그 구멍으로 다른 데를
건드리지 못합니다.

---

## 6. 폐기 절차

```sql
-- Oracle (DBA)
DROP USER TYSLACK_BOT;
DROP USER TYSLACK CASCADE;      -- 뷰 4개가 함께 사라진다
```
```bash
# 봇서버
sudo rm -f /etc/tybot/oracle.env
sudo systemctl disable --now tybot-sync.timer tybot-schedule.timer
```
방화벽 규칙 1건 회수. `TCP.INVITED_NODES` 를 썼다면 봇서버 IP 제거.
