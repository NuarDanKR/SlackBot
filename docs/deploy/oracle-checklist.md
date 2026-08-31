# Oracle 연동 — 담당자 실행 체크리스트

_2026-08-31 · 설계 근거는 [`../design/oracle-sync.md`](../design/oracle-sync.md)_

가져오는 것은 **조직 트리와 사번·이름·이메일·소속·직무뿐**이다.
비밀번호·휴대폰·전화·주소·생년월일은 가져오지 않는다.

---

## 진행 상황

| 단계 | 상태 |
|---|---|
| 0. 버전·접속 확인 | **완료** — 12.1.0.2.0 / `172.16.10.20:1523` **SID=BPROD** (서비스명 아님) |
| 1. 원본 구조 확인 | **완료** — `COVI_SMART4J.SYS_OBJECT_GROUP` · `SYS_OBJECT_USER` |
| 2. 전용 뷰 만들기 | **SQL 준비됨** → [`../../deploy/sql/oracle_tyslack_setup.sql`](../../deploy/sql/oracle_tyslack_setup.sql) · 실행은 담당자 |
| 3. 읽기 전용 계정 | 대기 — 같은 파일 3절 |
| 4. 추출 시험 | 대기 |
| 5. 전달 방식(A/B) | 대기 — 결정 필요 |
| 6. 퇴직자 처리 | **확인됨** — 아래 참고 |

### 확인된 사실 (2026-08-31, 실제 조회)

| 항목 | 값 |
|---|---|
| 부서(`GROUPTYPE='Dept'`) | 사용중 324 · 미사용 1047 |
| 사용자 | 사용중 1299 · 미사용 3068 |
| 인사연동 대상(`ISHR='Y'` + 재직) | 1120명 (소속 있음 1113 · 이메일 있음 1076) |
| 이메일 | 전부 `taeyoung.com`, 중복 0. 사번 중복도 0 |
| 조직 경로 구분자 | 세미콜론 `;` — `ORGROOT;TY;ABB300;ABB340;` |
| 계열사 | `COMPANYCODE` 로 구분. TY(태영건설) 241 · SUB01/03/06 · SPC01~24 |
| 트리 건전성 | 고아 0 · 순환 0 · 최대 깊이 4 · 상위가 미사용인 사용중 조직 0 |

**퇴직자는 행이 남지만 `ISHR` 이 'N' 으로 바뀐다.** 미사용 3068명 중 `ISHR='Y'` 는 9명뿐이다.
즉 `ISHR='Y'` 로 거르면 퇴직자는 **스냅샷에서 아예 사라진다.**
그래서 반영 잡은 "이번 스냅샷에 없는 사번 = `active=false`" 로 처리해야 한다.
안 그러면 퇴직자가 계속 조회 권한을 갖는다.

**계열사에는 인사연동 인원이 없다.** `ISHR='Y'` 재직자 1113명은 전원 TY 소속이다.
계열사 조직은 트리에만 있고 사람은 없다.

---

## 1단계. 기존 뷰에 어떤 컬럼이 있는지 본다

전용 뷰를 만들기 전에 원본에 무엇이 있는지 확인한다.
이름은 환경마다 다르므로 실제 뷰·테이블 이름으로 바꿔서 실행한다.

**도구로 대신할 수 있다.** `.env` 에 접속 정보를 넣으면(형식은 `.env.example` 의
`--- 레거시 Oracle ---` 절) 아래로 같은 일을 한다. `SELECT` 만 실행하고,
민감해 보이는 컬럼은 이름만 보여주고 값은 조회하지 않는다.

```bash
python scripts/oracle_probe.py                    # 조직·인사로 보이는 객체 찾기
python scripts/oracle_probe.py --table HR_DEPT    # 그 객체의 컬럼 + 행 수
python scripts/oracle_probe.py --sample HR_EMP    # 표본 5행(전부 마스킹)
python scripts/oracle_probe.py --tree HR_DEPT --code DEPT_CD --parent UP_DEPT_CD --name DEPT_NM
```

```sql
-- 조직도·인사 관련 객체 찾기
SELECT owner, object_name, object_type
  FROM all_objects
 WHERE object_type IN ('TABLE', 'VIEW')
   AND (object_name LIKE '%ORG%' OR object_name LIKE '%DEPT%'
        OR object_name LIKE '%EMP%' OR object_name LIKE '%HR%')
 ORDER BY owner, object_name;

-- 찾은 객체의 컬럼 보기
SELECT column_name, data_type, nullable
  FROM all_tab_columns
 WHERE owner = '<소유자>' AND table_name = '<객체명>'
 ORDER BY column_id;
```

**여기서 확인할 것**

1. 조직 코드·조직명·**상위 조직 코드**가 있는가 → 없으면 조직 트리를 만들 수 없다
2. 조직 구분(본부/팀/현장/프로젝트)을 나타내는 컬럼이 있는가
3. 사용 여부(`USE_YN`, `DEL_YN`, `STATUS` 등) 컬럼이 있는가 → **이게 제일 중요하다.**
   퇴직자·폐지조직을 행 삭제로 처리하는 시스템이면 방식이 달라진다(6단계)
4. 인사 뷰에 이메일이 있는가 → Slack 계정과 잇는 유일한 열쇠다

---

## 2단계. 전용 뷰 2개를 만든다

**기존 뷰를 그대로 쓰지 않는다.** 인사 뷰에는 주민번호·연락처·급여가 섞여 있기 마련이고,
그건 아카이브 금지 대상이다. 필요한 컬럼만 담은 뷰를 따로 만들어 그것만 권한을 준다.

### 별도 스키마를 못 만드는 경우 — 그룹웨어 스키마 안에 만든다

전용 스키마가 있으면 가장 깔끔하지만, 권한이 안 되면 **기존 그룹웨어 스키마 안에
`TYBOT_` 접두사를 붙여 만든다.** 접두사를 붙이는 이유는 두 가지다.

1. 그룹웨어 개발자가 나중에 "이건 뭐지" 하고 지우지 않게 — 소유가 드러나야 한다
2. 이름 충돌을 막는다 — 남의 스키마에 물건을 두는 것이므로 실수로 덮어쓰면 안 된다

**뷰가 테이블보다 낫다.** 테이블로 복제하면 (1) PII 가 한 벌 더 생기고, (2) 원본과
어긋나는 시점이 생기며, (3) 그걸 채우는 배치를 또 관리해야 한다. 뷰는 저장하는 게 없어서
셋 다 없다. 그룹웨어 스키마 용량에도 영향이 없으니 승인받기도 쉽다.

DDL 자체가 아예 불가능하면 뷰 없이도 간다 — 4단계 추출 SQL 의 `FROM` 을 원본 객체로
바꾸고 컬럼 화이트리스트를 그 SQL 안에서 고르면 된다. 다만 그러면 **"뭘 가져가는지"가
DB 에 남지 않아** 나중에 감사에서 설명하기 어렵다. 가능하면 뷰로 만든다.

컬럼 이름은 1단계에서 확인한 실제 이름으로 바꾼다. **오른쪽 별칭은 그대로 둔다**
(추출 SQL 이 이 이름을 쓴다). 아래 예시는 이름을 `TYBOT_V_ORG` / `TYBOT_V_EMP` 로
바꿔 쓰고, 그 경우 추출 SQL 의 `FROM V_TYSLACK_ORG` 도 같이 바꾼다.

```sql
CREATE OR REPLACE VIEW V_TYSLACK_ORG AS
SELECT <조직코드>      AS org_code,
       <조직명>        AS org_name,
       <상위조직코드>  AS parent_org_code,
       <조직구분>      AS org_kind,      -- hq | team | site | project 로 매핑 가능한 값
       <사용여부>      AS use_yn         -- 'Y' / 'N'
  FROM <기존 조직도 뷰>;

CREATE OR REPLACE VIEW V_TYSLACK_EMP AS
SELECT <사번>       AS emp_no,
       <성명>       AS emp_name,
       <이메일>     AS email,
       <소속조직코드> AS org_code,
       <직위>       AS position_name,
       <재직여부>   AS use_yn
  FROM <기존 인사기본 뷰>;
```

만든 뒤 눈으로 확인한다.

```sql
SELECT * FROM V_TYSLACK_ORG WHERE ROWNUM <= 20;
SELECT * FROM V_TYSLACK_EMP WHERE ROWNUM <= 20;

-- 건수 감각 (나중에 '스냅샷이 너무 작으면 중단' 기준값이 된다)
SELECT count(*) AS org_cnt FROM V_TYSLACK_ORG;
SELECT count(*) AS emp_cnt FROM V_TYSLACK_EMP;
SELECT count(*) AS emp_with_email FROM V_TYSLACK_EMP WHERE email IS NOT NULL;
```

**`emp_with_email` 이 전체의 절반도 안 되면 알려달라.** 이메일이 없으면 Slack 계정과
이을 수 없어서, 사번 매핑을 다른 방식(OTP·수동)으로 설계해야 한다.

### 조직 트리가 실제로 이어지는지 확인

부모 코드가 끊겨 있으면 권한 상속이 조용히 어긋난다. **가장 흔한 사고 지점이다.**

```sql
-- 부모가 없는(고아) 조직
SELECT o.org_code, o.org_name, o.parent_org_code
  FROM V_TYSLACK_ORG o
 WHERE o.parent_org_code IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM V_TYSLACK_ORG p WHERE p.org_code = o.parent_org_code);

-- 최상위(부모 없음)가 몇 개인가 — 보통 1개여야 한다
SELECT count(*) FROM V_TYSLACK_ORG WHERE parent_org_code IS NULL;

-- 순환 참조가 있는지 (A→B→A). 있으면 재귀 조회가 무한히 돈다
SELECT org_code FROM V_TYSLACK_ORG
 START WITH parent_org_code IS NULL
 CONNECT BY NOCYCLE PRIOR org_code = parent_org_code
 AND CONNECT_BY_ISCYCLE = 1;
```

고아·순환이 나오면 **결과를 알려달라.** 뷰에서 걸러낼지, 우리 쪽에서 처리할지 정해야 한다.

---

## 3단계. 읽기 전용 계정을 만든다

```sql
CREATE USER TYSLACK_BOT IDENTIFIED BY "<강한 비밀번호>";

GRANT CREATE SESSION TO TYSLACK_BOT;
-- 그룹웨어 스키마 안에 만들었다면 소유자를 붙인다: GRANT SELECT ON GROUPWARE.TYBOT_V_ORG TO ...
GRANT SELECT ON V_TYSLACK_ORG TO TYSLACK_BOT;
GRANT SELECT ON V_TYSLACK_EMP TO TYSLACK_BOT;

-- 그 외 어떤 권한도 주지 않는다.
-- CONNECT / RESOURCE 롤도 주지 않는다 — 필요 이상의 권한이 딸려온다.
```

권한이 정확히 이 둘뿐인지 확인한다.

```sql
SELECT * FROM dba_sys_privs  WHERE grantee = 'TYSLACK_BOT';
SELECT * FROM dba_tab_privs  WHERE grantee = 'TYSLACK_BOT';
SELECT granted_role FROM dba_role_privs WHERE grantee = 'TYSLACK_BOT';
```

**비밀번호는 채팅·메일·이슈에 붙여넣지 않는다.** 서버의 설정 파일에만 넣는다.

---

## 4단계. 추출이 되는지 시험한다

저장소에 스크립트가 있다.

**12.1 이므로 `_12_1` 이 붙은 파일을 쓴다.**

```bash
export NLS_LANG=KOREAN_KOREA.AL32UTF8

sqlplus -s TYSLACK_BOT/<비밀번호>@<호스트>:1521/<서비스명> @deploy/sql/export_org_12_1.sql > org.jsonl
sqlplus -s TYSLACK_BOT/<비밀번호>@<호스트>:1521/<서비스명> @deploy/sql/export_emp_12_1.sql > emp.jsonl

head -3 org.jsonl
wc -l org.jsonl emp.jsonl
```

나온 파일이 진짜 JSON 인지 한 줄씩 확인한다. **눈으로 보면 멀쩡해 보여도 특수문자 하나에
깨질 수 있으므로 기계로 검사한다.**

```bash
python3 -c "
import json,sys
bad=0
for i,l in enumerate(open('org.jsonl',encoding='utf-8'),1):
    if not l.strip(): continue
    try: json.loads(l)
    except Exception as e: print(i, e); bad+=1
print('깨진 줄', bad, '개')
"
```

한 줄에 JSON 하나씩 나오면 성공이다.

```json
{"org_code":"ABB540","org_name":"자금팀","parent_code":"HQ","kind":"team","active":true}
```

**한글이 깨지면** 추출하는 쉘의 문자셋을 맞춘다.

```bash
export NLS_LANG=KOREAN_KOREA.AL32UTF8
```

`ORA-00904: "JSON_OBJECT": invalid identifier` 가 나오면 `_12_1` 이 안 붙은 파일을
실행한 것이다. 파일 이름을 다시 확인한다.

---

## 5단계. 전달 방식을 정한다 — 결정 지점

| | 방식 A: 봇서버 → Oracle 직접 조회 | 방식 B: 내부망 → 봇서버로 파일 전송 |
|---|---|---|
| 방화벽 | 봇서버에서 Oracle 로 나가는 구멍(1521) | 봇서버로 들어오는 파일 한 방향 |
| 봇서버가 털리면 | **Oracle 자격증명이 함께 나간다** | 스냅샷 파일 한 장뿐 |
| 스케줄 통제 | 봇서버가 통제 | 내부망 배치가 통제 |
| 설계 문서 권고 | | **이쪽** |
| 12.1 에서 가능한가 | 가능(thin 모드 지원) | 가능 |

방식 A 로 가면 방화벽 신청에 이렇게 적는다.

```
출발지: <봇서버 IP>      목적지: <Oracle IP>   포트: 1521/tcp
용도  : 조직도·인사기본 뷰 조회 (SELECT only), 야간 1회 + 수동 트리거
계정  : TYSLACK_BOT (읽기 전용, 뷰 2개만 GRANT)
```

방식 B 로 가면 내부망 배치 서버에서 4단계 명령을 돌리고, 나온 `org.jsonl`·`emp.jsonl`
두 파일을 봇서버로 보낸다(SFTP 등). 봇은 파일이 도착하면 읽어 반영한다.

---

## 6단계. 반영 전에 정해야 할 것 — 퇴직자·폐지조직 처리

**이게 가장 조용한 사고 지점이다.**

- 원본이 `USE_YN='N'` 으로 표시만 한다면 → 그대로 받아 `active=false` 로 넣으면 된다
- 원본이 **행을 삭제**한다면 → 스냅샷에서 사라진 사번을 우리가 `active=false` 로 바꿔야 한다.
  안 그러면 퇴직자가 계속 조회 권한을 갖는다

1단계에서 확인한 결과를 알려주면 반영 잡을 그에 맞게 만든다.

또한 반영은 **전량 교체를 한 트랜잭션 안에서** 한다. 절반만 반영되면 조직 트리가 끊겨
권한 판정이 엉킨다. 원본 조회가 실패해 빈 스냅샷이 오는 경우를 '전원 퇴직'으로 오인하지
않도록, **직전 대비 건수가 급감하면 반영을 중단**하는 장치를 함께 넣는다.

---

## 받는 쪽은 준비돼 있다

PostgreSQL 에 테이블이 이미 만들어져 있다(`deploy/sql/index_schema.sql`, 2026-08-30 적용 완료).

| 테이블 | 담는 것 |
|---|---|
| `org_unit` | 조직 트리 (`V_TYSLACK_ORG` 복제) |
| `employee` | 사번·이름·이메일·소속 (`V_TYSLACK_EMP` 복제) |
| `user_identity` | Slack 계정 ↔ 사번 매핑 (우리가 만드는 데이터) |
| `sync_run` | 동기화 이력 — 실패 추적용 |

조직 트리 재귀 조회와 이메일 대소문자 무시 매칭은 실제 DB 에서 동작을 확인했다.

---

## 정리 — 남은 것

- [x] ~~버전~~ → 12.1.0.2.0 · SID `BPROD`
- [x] ~~조직·인사 원본 구조~~ → `SYS_OBJECT_GROUP` / `SYS_OBJECT_USER`
- [x] ~~퇴직자를 어떻게 표시하는가~~ → `ISHR` 이 'N' 으로 바뀐다(행은 남는다)
- [x] ~~건수·고아·순환~~ → 위 표
- [ ] **뷰 2개 생성** — `oracle_tyslack_setup.sql` 실행. 만들 스키마를 정한다
- [ ] **`TYSLACK_BOT` 계정 생성 + 뷰 2개에만 GRANT**
- [ ] `NLS_CHARACTERSET` 확인(추출 인코딩용)
- [ ] 방식 A(직접 조회) / B(파일 전달) 결정

이 넷이 끝나면 반영 잡(B-04)을 만든다.

**막히면 그 지점의 오류 메시지를 그대로 알려달라.** 12.1 은 최신 문법이 없는 경우가 있어,
`ORA-` 번호만 있으면 대체 문법을 만들 수 있다.
