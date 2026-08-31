# 그룹웨어 알림 → Slack DM 중계

_2026-08-31 · 실제 DB(BPROD) 조사 결과 포함 · 미착수(설계만)_
_전제: [oracle-sync.md](oracle-sync.md) · [infra-request-snapshot-push.md](../deploy/infra-request-snapshot-push.md)_

그룹웨어가 사내 메신저로 보내던 결재·업무 알림을 봇이 Slack DM 으로도 전달한다.
봇의 골조가 "종합 비서" 이므로, 사람이 여러 창을 보지 않게 하는 것이 목적이다.

---

## 0. 가장 먼저 — 절대 하면 안 되는 것

**`COVI_SMART4J.MSG_SND` 에 쓰지 않는다. `SND_Y_N` 을 'Y' 로 바꾸지 않는다.**

그 컬럼은 **기존 메신저의 소비 표시**다. 두 소비자가 같은 플래그를 공유하면 각 알림이
둘 중 **하나에게만** 간다 — 우리가 먼저 집은 건 메신저가 못 보내고, 반대도 마찬가지다.
사용자에게는 "가끔 메신저 알림이 안 온다" 로 나타나고, 원인 추적이 대단히 어렵다.

우리는 **읽기만 하고, 어디까지 읽었는지는 우리 DB 에 따로 기록**한다.

---

## 1. 조사 결과 (2026-08-31, 실측)

### 테이블 구조

| 컬럼 | 타입 | 쓰임 |
|---|---|---|
| `MSG_ID` | VARCHAR2(50) **PK** | 유일 키. 접두 `A`(결재) / `P`(PLC) / `E`(EBS) + 0 패딩 |
| `SYS_NAME` | VARCHAR2(20) | 알림 종류 |
| `MSG_CODE` | VARCHAR2(10) | 세부 코드(미조사) |
| `SND_USER` | VARCHAR2(20) | 발신 사번 |
| `RCV_USER` | VARCHAR2(2000) | 수신 사번. **여러 명이면 쉼표로 이어진다** |
| `DOC_NAME` | VARCHAR2(100) | 알림 제목 |
| `DOC_URL` | VARCHAR2(1000) | 링크 |
| `DOC_DESC` | VARCHAR2(2000) | 본문 |
| `ALARM_TYPE` | VARCHAR2(50) | (미조사) |
| `WRITE_DATE` | VARCHAR2(17) | 생성 시각. **실제 값은 항상 14자 `YYYYMMDDHH24MISS`** |
| `SND_DATE` / `READ_DATE` | VARCHAR2(17) | 메신저가 채움 |
| `SND_Y_N` | VARCHAR2(10) | **메신저의 소비 표시. 건드리지 않는다** |

### 숫자

| 항목 | 값 |
|---|---|
| 전체 행 | 2,010,994 |
| `SND_Y_N='N'` (미발송) | **0** — 메신저가 즉시 소비한다 |
| 종류별 | 결재 1,239,209 · PLC 752,632 · EBS 19,153 |
| 하루 건수 | 평일 약 1,000~1,200건 (주말 100~170) |
| 시간당 최대 | 약 200건 |
| `WRITE_DATE` 범위 | 2020-01-20 ~ 현재 |
| 다중 수신 행 | 1,824건(길이 20자 초과), 쉼표 포함 13,987건 |
| 인덱스 | **`MSG_SND_PK` (MSG_ID) 하나뿐** |

### 여기서 갈리는 것

**인덱스가 PK 하나뿐이다.** `WRITE_DATE` 로 30초마다 조회하면 200만 행 전체 스캔을
반복한다 — 그룹웨어 DB 에 부담이 간다.

**대신 `MSG_ID` 로 훑는다.** `MSG_ID` 는 접두문자 + 0 패딩 고정폭(50자)이라
문자열 정렬 = 숫자 정렬이다. 그러면 PK 인덱스를 그대로 쓸 수 있다:

```sql
SELECT ... FROM MSG_SND
 WHERE MSG_ID > :watermark AND MSG_ID LIKE 'A%'
 ORDER BY MSG_ID
 FETCH FIRST 500 ROWS ONLY
```

접두가 3종(A/P/E)이므로 **워터마크도 3개**를 둔다.
→ **COVI 테이블에 인덱스를 추가할 필요가 없다.** DDL 요청이 0건이 된다.

> **착수 전 반드시 확인**: `MSG_ID` 의 0 패딩이 정말 고정폭인지, 그리고 시간순으로
> 증가하는지. 표본에서 앞 20자가 전부 0이라 뒷자리를 직접 봐야 한다.
> 아니면 이 설계 전체가 무너지므로 여기부터 검증한다.

---

## 2. 커밋 순서 문제 — 놓치는 알림이 생기는 자리

`MSG_ID` 는 **커밋 전에** 정해진다. 트랜잭션이 늦게 커밋되면, 우리가 이미 지나간
번호대의 행이 뒤늦게 나타난다. 워터마크를 그대로 올려 두면 **그 알림은 영원히 안 간다.**

대응: **워터마크를 최신에 붙이지 않고 조금 뒤에 둔다.**

- `WRITE_DATE` 가 현재보다 10초 이상 지난 행만 소비한다
- 그리고 우리 쪽에 `MSG_ID` 유일 제약을 걸어 중복 전송을 막는다

지연 10초 + 폴링 30초 = 최대 40초 안팎. 결재 알림 용도로는 충분하다.
"놓치는 것보다 늦는 것이 낫다" 쪽으로 정한 것이다.

---

## 3. 전달 경로 — 새 방화벽 규칙 없이

봇 서버는 공인 IP DMZ 장비다. 거기서 사내 Oracle 로 들어가는 규칙은 만들지 않는다
(그 이유는 [인프라 요청서 1절](../deploy/infra-request-snapshot-push.md)).

**이미 신청한 SFTP 경로를 그대로 쓰고 주기만 줄인다.**

```
[내부망 배치서버]                          [DMZ 봇서버]
 msg_snd 조회(30초)                         inbox/notify/ 감시(10초)
   → notify-<타임스탬프>.jsonl               → Slack DM 발송
   → SFTP 업로드 ────────────────────────▶  → 처리 후 processed/ 이동
```

- 조직 스냅샷과 **같은 계정·같은 inbox**, 하위 폴더만 `notify/` 로 나눈다
- **신규 방화벽 규칙 0건.** 지금 심사 올린 건이 그대로 쓰인다
- SSH 핸드셰이크 비용은 `ControlMaster`/`ControlPersist` 로 연결을 재사용해 줄인다

### 왜 상시 연결(HTTPS/WebSocket)이 아닌가

지연을 1~2초로 줄이려면 봇 서버에 **수신 엔드포인트**가 필요하다. 그러면
(1) 방화벽 규칙이 추가되어 심사가 처음부터 다시 가고,
(2) 봇이 인바운드 포트를 여는 것이라 [CLAUDE.md 의 Socket Mode 원칙](../../CLAUDE.md)에
어긋난다. 결재 알림에 40초 지연은 문제가 아니다. **필요해지면 그때 바꾼다.**

---

## 4. 받는 쪽 설계

### 새 테이블 (PostgreSQL)

```sql
-- 어디까지 읽었는지. 접두(A/P/E)별로 하나씩.
CREATE TABLE IF NOT EXISTS notify_watermark (
    stream      text PRIMARY KEY,          -- 'A' | 'P' | 'E'
    last_msg_id text NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- 보낸 것·못 보낸 것 이력. **재빌드 불가 — 백업 대상.**
CREATE TABLE IF NOT EXISTS notification (
    msg_id      text NOT NULL,             -- 원본 MSG_ID
    rcv_emp_no  text NOT NULL,             -- 쉼표 분리 후 1행 1명
    sys_name    text,
    snd_emp_no  text,
    title       text,
    url         text,
    written_at  timestamptz,
    slack_user  text,
    status      text NOT NULL
        CHECK (status IN ('pending','sent','no_account','expired','failed')),
    fail_reason text,
    delivered_at timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now(),
    -- 같은 알림을 같은 사람에게 두 번 보내지 않는다. 이 제약이 유일한 방어선이다.
    PRIMARY KEY (msg_id, rcv_emp_no)
);
CREATE INDEX IF NOT EXISTS notification_pending
    ON notification (created_at) WHERE status = 'pending';
```

### 수신자 매핑

`RCV_USER`(사번) → `employee.emp_no` → `user_identity.slack_user`.

- **매핑이 없으면 보내지 않는다.** 추측하지 않는다 — 사번을 잘못 맞히면 남의 결재 알림이
  엉뚱한 사람에게 간다. `status='no_account'` 로 남긴다
- 재직자 1,120명 중 **이메일 없는 44명**은 Slack 매칭 열쇠가 없다(B-05 과제)
- 쉼표 분리 후 각 수신자를 **별도 행**으로 만든다. 한 명이 실패해도 나머지는 간다

### 밀린 알림 처리 — 복구 시 폭탄 방지

봇이 몇 시간 죽었다 살아나면 밀린 알림이 쌓인다. **한 사람에게 200건을 순서대로 쏘면
그 자체가 장애다.** 규칙:

- `written_at` 이 **6시간** 넘게 지난 건은 개별 발송하지 않고 `status='expired'`
- 대신 "그동안 받은 알림 N건" 요약 1건을 보내고 그룹웨어 링크를 붙인다
- 한 사람당 한 번에 보내는 개별 알림 상한 **10건**, 넘으면 요약으로 전환

### 아카이브 금지

이 알림은 **개인에게 가는 데이터다. MD 아카이브에 저장하지 않는다.**
([CLAUDE.md 원칙 1·5](../../CLAUDE.md)) `notification` 테이블에만 남고,
그 테이블은 답변 근거로 검색되지 않는다.

로그에도 `title`·`url` 을 평문으로 남기지 않는다. 건수와 `msg_id` 만 남긴다.

---

## 5. 링크가 열리는지 확인할 것

`DOC_URL` 은 사내 그룹웨어 주소일 가능성이 높다. Slack 모바일에서 사외망으로 열면
접속되지 않는다. 확인해서:

- 열린다면 그대로 붙인다
- 안 열린다면 링크를 빼고 제목만 보내거나, "그룹웨어에서 확인" 문구를 붙인다

**깨진 링크를 그대로 보내면 사용자는 봇이 고장난 것으로 받아들인다.**

---

## 6. 할 일 순서

1. **`MSG_ID` 단조성 검증** (1절 경고 참고). 아니면 설계를 `WRITE_DATE` + 인덱스 추가로 변경
2. `MSG_CODE`·`ALARM_TYPE` 값 분포 조사 — 보낼 종류를 고를지 결정
3. `DOC_URL` 접속 가능성 확인 (5절)
4. Oracle 에 알림 전용 뷰 `V_TYSLACK_MSG` 추가 + `TYSLACK_BOT` 에 SELECT GRANT
   (원본 테이블 권한은 계속 주지 않는다)
5. 보내는 쪽: `scripts/notify_export.py` — 워터마크 조회 → JSONL → SFTP
   (워터마크는 배치서버 로컬 파일에 둔다. 봇 DB 에 물어보러 갈 수 없다)
6. 받는 쪽: `src/tybot/notify.py` — 파일 읽기 → 매핑 → DM 발송 → 이력 기록
7. 테스트: 중복 발송 차단, 매핑 실패, 밀린 알림 요약 전환, 다중 수신자 분리

---

## 7. 정해야 할 것

- **알림 종류를 전부 보낼 것인가.** PLC 752K건이 어떤 성격인지 모른다.
  결재만 먼저 켜고 나머지는 뒤에 붙이는 편이 안전하다
- **수신 거부를 어떻게 줄 것인가.** 그룹웨어 알림을 봇이 다시 보내면 이중 알림이다.
  사용자별 on/off 가 없으면 불만이 쌓인다
- **메신저를 대체할 것인가, 병행할 것인가.** 병행이면 이중 알림을 감수하는 것이고,
  대체면 메신저 쪽 발송을 꺼야 하는데 그건 그룹웨어 운영 변경이다
