# 그룹웨어 Oracle → TYBot PostgreSQL 동기화

_설계 문서 · 2026-08-21 · 조직도·인사기본 뷰를 DMZ 봇 서버로 가져오는 방법_

전제: 그룹웨어 Oracle 에 **조직도 뷰**와 **인사기본 뷰**(메일·사번 등)가 이미 있다.
목표: 그 정보를 봇 서버의 PostgreSQL 로 옮겨 `slack_user ↔ 사번 ↔ 조직` 매핑을 만든다.

---

## 0. 방향이 먼저다 — 두 방식

관건은 "어떻게 연결하나"가 아니라 **누가 연결을 시작하나**다. 보안 심사가 여기서 갈린다.

### 방식 A. DMZ → 내부망 직접 조회 (pull)
```
[DMZ 봇서버] --1521/tcp--> [내부망 Oracle]
```
| | |
|---|---|
| 장점 | 구현 단순. 스케줄·재시도를 봇 서버가 통제 |
| 단점 | **DMZ 에서 내부망으로 들어가는 구멍**을 방화벽에 뚫어야 한다 |
| 필요 | 목적지 IP:1521 화이트리스트, 읽기전용 계정, 뷰 단위 GRANT |

### 방식 B. 내부망 → DMZ 스냅샷 밀어넣기 (push) — **권장**
```
[내부망 배치서버] --22/tcp(SFTP)--> [DMZ 봇서버]
```
내부망 서버가 뷰를 조회해 JSON/CSV 스냅샷을 만들고, DMZ 로 **밀어 넣는다**.
봇 서버는 파일이 도착하면 읽어서 PostgreSQL 에 반영한다.

| | |
|---|---|
| 장점 | **DMZ→내부망 연결이 0개.** 기존 보안 경계(인바운드 없음 · 단방향)를 유지 |
| | 봇 서버가 털려도 Oracle 자격증명이 없다 — 유출 자산이 스냅샷 한 장뿐 |
| 단점 | 내부망 쪽에 배치 스크립트를 둬야 한다. 스케줄 통제권이 양쪽으로 나뉜다 |

**권고: B.** 우리 아키텍처는 이미 "인바운드 0개 · DMZ→내부망 단방향 화이트리스트"를 전제로 한다.
A 는 그 원칙을 정면으로 깨고, 심사에서도 A 가 더 어렵다. B 는 파일 한 방향이라 설명도 쉽다.

두 방식 모두 아래 3~6절(스키마·반영·안전장치)은 동일하다.

---

## 1. Oracle 쪽 준비 — 전용 뷰와 최소 권한

기존 뷰를 그대로 쓰지 말고, **필요한 컬럼만 담은 전용 뷰**를 하나 더 만들어 달라고 요청한다.
이유: 인사 뷰에는 주민번호·연락처·급여·평가 같은 컬럼이 섞여 있기 마련이고, 그건 아카이브 금지 대상이다.

```sql
-- DBA 에게 요청할 뷰 (컬럼 화이트리스트)
CREATE OR REPLACE VIEW V_TYBOT_ORG AS
SELECT org_code, org_name, parent_org_code, org_kind, use_yn
  FROM <기존 조직도 뷰>;

CREATE OR REPLACE VIEW V_TYBOT_EMP AS
SELECT emp_no, emp_name, email, org_code, position_name, use_yn
  FROM <기존 인사기본 뷰>;

-- 읽기 전용 계정
CREATE USER TYBOT_RO IDENTIFIED BY "<강한 비밀번호>";
GRANT CREATE SESSION TO TYBOT_RO;
GRANT SELECT ON V_TYBOT_ORG TO TYBOT_RO;
GRANT SELECT ON V_TYBOT_EMP TO TYBOT_RO;
-- 그 외 어떤 권한도 주지 않는다(DML·DDL·다른 테이블 SELECT 금지)
```

**가져오지 않는 것**: 주민번호, 개인 연락처, 주소, 급여, 인사평가, 가족관계.
필요해질 때 추가하는 게 아니라, **필요한 것만 늘려가는** 방향으로 유지한다.

`use_yn` 이 중요하다 — 퇴직자·폐지조직을 삭제로 처리하면 권한 판정이 조용히 넓어지거나 좁아진다(6절).

---

## 2-A. 방식 A(pull) 구현 — python-oracledb

```bash
# Instant Client 불필요. thin 모드는 순수 파이썬이다.
/opt/tybot/.venv/bin/pip install oracledb
```

```python
import oracledb
conn = oracledb.connect(user="TYBOT_RO", password=..., dsn="host:1521/SERVICE")
```

**먼저 확인할 것 — thin 모드 요건**
| 항목 | 조건 |
|---|---|
| Oracle DB 버전 | **12.1 이상**. 11g 이면 thin 모드 불가 → Instant Client + thick 모드 필요 |
| 인증 방식 | 기본 비밀번호 인증이면 OK. Kerberos·wallet 은 thick 모드 |
| 문자셋 | thin 모드가 UTF-8 로 변환한다. KO16MSWIN949 DB 여도 파이썬에선 정상 |

11g 라면 thick 모드가 필요하고 그러면 서버에 Instant Client RPM 을 깔아야 한다 —
**그 시점에서 방식 B 가 더 싸다.**

방화벽 요청서에 적을 내용:
```
출발지: <DMZ 봇서버 IP>
목적지: <Oracle IP>  포트: 1521/tcp
용도  : 조직도·인사기본 뷰 조회(SELECT only), 야간 1회 + 수동 트리거
계정  : TYBOT_RO (읽기 전용, 뷰 2개만 GRANT)
```

## 2-B. 방식 B(push) 구현 — 스냅샷 파일

내부망 배치서버에서 (기존 사내 배치 도구·쉘·SQL*Plus 무엇이든):

```bash
# 예: SQL*Plus 로 JSON 한 줄씩 뽑고 SFTP 로 밀어넣기
sqlplus -s TYBOT_RO/****@ORCL @export_tybot.sql > /tmp/tybot_org.jsonl
sqlplus -s TYBOT_RO/****@ORCL @export_emp.sql > /tmp/tybot_emp.jsonl

sha256sum /tmp/tybot_*.jsonl > /tmp/tybot.sha256
sftp -i /path/key tybot_ingest@<DMZ서버> <<'EOF'
put /tmp/tybot_org.jsonl /var/lib/tybot/inbox/
put /tmp/tybot_emp.jsonl /var/lib/tybot/inbox/
put /tmp/tybot.sha256    /var/lib/tybot/inbox/
EOF
```

DMZ 쪽 준비:
- 전용 계정 `tybot_ingest`, **SFTP 전용**(`ForceCommand internal-sftp`, `ChrootDirectory`)
- 쓰기 가능 경로는 `/var/lib/tybot/inbox` 하나
- 봇 서버는 `sha256` 검증 후 반영. 검증 실패면 반영하지 않고 경고

> 이 방식의 핵심 이점: **DMZ 서버에 Oracle 자격증명이 존재하지 않는다.**

---

## 3. PostgreSQL 스키마

```sql
-- 조직 트리 (Oracle 이 원본, 우리는 읽기 복제)
CREATE TABLE org_unit (
  code        TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  kind        TEXT NOT NULL,              -- hq | team | site | project
  parent_code TEXT REFERENCES org_unit(code),
  active      BOOLEAN NOT NULL DEFAULT TRUE,
  synced_at   TIMESTAMPTZ NOT NULL
);

-- 인사기본 (사번·이름·이메일·소속)
CREATE TABLE employee (
  emp_no    TEXT PRIMARY KEY,
  name      TEXT NOT NULL,
  email     TEXT,
  org_code  TEXT REFERENCES org_unit(code),
  position  TEXT,
  active    BOOLEAN NOT NULL DEFAULT TRUE,
  synced_at TIMESTAMPTZ NOT NULL
);
CREATE UNIQUE INDEX employee_email_key ON employee (lower(email)) WHERE email IS NOT NULL;

-- Slack 사용자 ↔ 사번 매핑 (Oracle 이 아니라 우리가 만드는 데이터)
CREATE TABLE user_identity (
  workspace   TEXT NOT NULL,
  slack_user  TEXT NOT NULL,
  emp_no      TEXT REFERENCES employee(emp_no),
  verified_by TEXT NOT NULL,              -- email_match | otp | sso | manual
  verified_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (workspace, slack_user)
);

-- 동기화 이력 (실패 추적용 - 재빌드 불가, 백업 대상)
CREATE TABLE sync_run (
  id         BIGSERIAL PRIMARY KEY,
  source     TEXT NOT NULL,               -- oracle_pull | snapshot_push
  started_at TIMESTAMPTZ NOT NULL,
  ended_at   TIMESTAMPTZ,
  ok         BOOLEAN,
  org_rows   INT, emp_rows INT,
  message    TEXT
);
```

매핑은 이메일 조인 한 줄로 생긴다:
```sql
INSERT INTO user_identity (workspace, slack_user, emp_no, verified_by, verified_at)
SELECT $1, $2, e.emp_no, 'email_match', now()
  FROM employee e
 WHERE lower(e.email) = lower($3) AND e.active
ON CONFLICT (workspace, slack_user) DO UPDATE
   SET emp_no = EXCLUDED.emp_no, verified_at = now();
```
`$3` 는 Slack `users.info` 의 `profile.email` 이다.

---

## 4. 반영 방식 — 스테이징 후 원자적 교체

부분 반영이 가장 위험하다. 조직 트리가 절반만 바뀌면 권한 판정이 엉킨다.

```sql
BEGIN;
  CREATE TEMP TABLE org_stage (LIKE org_unit) ON COMMIT DROP;
  -- (스테이지에 전량 적재)

  -- 스냅샷이 비정상적으로 작으면 중단한다(원본 조회 실패를 '전원 퇴직'으로 오인하지 않게)
  -- 애플리케이션에서 검사: stage_rows >= existing_rows * 0.9

  INSERT INTO org_unit AS o (code, name, kind, parent_code, active, synced_at)
  SELECT code, name, kind, parent_code, TRUE, now() FROM org_stage
  ON CONFLICT (code) DO UPDATE
     SET name = EXCLUDED.name, kind = EXCLUDED.kind,
         parent_code = EXCLUDED.parent_code, active = TRUE, synced_at = now();

  -- 사라진 행은 지우지 않고 비활성 처리한다(6절)
  UPDATE org_unit SET active = FALSE, synced_at = now()
   WHERE code NOT IN (SELECT code FROM org_stage);
COMMIT;
```

- 트랜잭션 하나로 끝낸다. 실패하면 **직전 스냅샷이 그대로 살아 있다**.
- `parent_code` 순환 참조는 반영 전 검사(재귀 CTE 로 깊이 제한 확인).

---

## 5. 스케줄

| 작업 | 주기 | 이유 |
|---|---|---|
| 조직·인사 동기화 | **야간 1회**(예: 03:30) + 수동 트리거 | 조직 정보는 하루 단위로 바뀐다. 실시간 조회는 내부망 부하·장애 전파 |
| Slack 이메일 매핑 | 동기화 직후 | 신규 입사자가 Slack 에 들어오면 다음 동기화에서 잡힌다 |

`tybot-sync.timer` 로 구성하고, **봇 프로세스와 분리**한다
([agent-architecture.md](agent-architecture.md) 4절 — 스케줄은 결정론적 잡이 맡는다).

---

## 6. 안전장치 (여기가 본론)

| 위험 | 대응 |
|---|---|
| Oracle 조회 실패 | **직전 스냅샷 유지 + 경고.** 빈 결과를 반영하지 않는다 |
| 스냅샷이 비정상적으로 작음 | 기존 행 수의 90% 미만이면 중단. "전원 퇴직"으로 오인 방지 |
| 퇴직자·폐지조직 | 삭제하지 않고 `active=false`. 이력이 남아야 과거 원문의 발화자를 해석할 수 있다 |
| 조직 정보 없음 | **상속 없이 자기 채널만**(막는 쪽 폴백). 조직을 모르면 권한을 넓히지 않는다 |
| 이메일 중복·불일치 | 매핑 실패로 남긴다. 추정하지 않는다 |
| PII 유입 | 뷰 컬럼 화이트리스트 + 반영 전 패턴 검사(주민번호 형식 등) |
| 자격증명 유출 | 방식 B 를 쓰면 DMZ 에 Oracle 계정이 없다. A 라면 `/etc/tybot/tybot.env`(0640) |

**원문은 건드리지 않는다.** 수집된 MD 의 발화자 표기는 그대로 두고, 매핑은 답변·요약·감사
표시에서만 적용한다(원문 불변).

---

## 7. 이 매핑이 여는 것 — 진짜 가치

`slack_user → emp_no → org_code` 가 확보되면 권한 판정이 한 단계 올라간다.

```
현재: 채널 멤버십 + 워크스페이스 화이트리스트 + root 플래그
이후: 조직 트리 상속 (본부 > 팀 > 현장) — 채널을 일일이 초대하지 않아도 계층으로 판정
```

[db-and-acl.md](db-and-acl.md) 4절의 상속 규칙(`org_public` / 상향은 공개분만 / 형제 차단)이
그때 비로소 동작한다. 이름 표기는 부산물이고, **조직 매핑이 목적**이다.

---

## 8. 진행 순서

| 단계 | 내용 | 담당 |
|---|---|---|
| 1 | 방식 A/B 결정 (보안 담당 협의) | 우리 + 보안 |
| 2 | `V_TYBOT_ORG` / `V_TYBOT_EMP` 뷰 + `TYBOT_RO` 계정 생성 | DBA |
| 3 | PostgreSQL 설치 + 3절 스키마 | 우리 |
| 4 | 동기화 잡 구현 (선택한 방식) + `sync_run` 이력 | 우리 |
| 5 | Slack 이메일 매핑 → `user_identity` | 우리 |
| 6 | 답변·감사 표기에 `이름(사번)` 적용 | 우리 |
| 7 | 조직 트리 상속 권한 판정으로 전환 | 우리 |

2번(뷰·계정)이 외부 의존이라 가장 오래 걸린다. **1번 결정과 2번 요청을 먼저 걸어두고**
3~5번을 병행하는 게 빠르다.

### DBA·보안에게 보낼 요청 요약
> 조직도·인사기본 정보를 사내 Slack 봇의 권한 판정에 쓰려고 합니다.
> - 필요한 컬럼만 담은 전용 뷰 2개(`V_TYBOT_ORG`, `V_TYBOT_EMP`) 생성 요청
>   — 조직코드/조직명/상위조직/구분/사용여부, 사번/이름/이메일/조직코드/직위/사용여부
>   — **주민번호·연락처·주소·급여·평가 컬럼은 포함하지 않습니다**
> - 그 뷰 2개에만 SELECT 권한을 가진 읽기전용 계정 1개
> - 연동 방향은 **내부망→DMZ 파일 푸시(방식 B)** 를 우선 제안합니다.
>   DMZ 에서 내부망으로 들어오는 방화벽 구멍이 필요 없고, DMZ 서버에 DB 자격증명이 남지 않습니다.
