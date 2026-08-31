-- TYSlack ↔ 그룹웨어(COVI) 연동 — Oracle 쪽 설치 SQL
--
-- 대상: BPROD (Oracle 12.1.0.2.0), 원본 스키마 COVI_SMART4J
-- 2026-08-31 · 실제 데이터를 확인하고 만든 매핑이다. 근거는
--              docs/deploy/oracle-checklist.md 의 "확인된 사실" 표.
--
-- ## 순서대로 세 부분이다. 실행 계정이 다르다.
--   A. DBA 계정으로     — 스키마·계정 생성과 원본 테이블 권한 부여
--   B. TYSLACK 계정으로 — 뷰 2개 생성, 봇 계정에 조회 권한 부여
--   C. 아무 계정으로    — 제대로 됐는지 확인
--
-- ## 왜 GWUSER 로 못 하는가 (2026-08-31 확인)
--   - `CREATE USER` 시스템 권한이 없다 → A 를 실행할 수 없다
--   - COVI 테이블 SELECT 가 `GRANTABLE=NO` 다 → 뷰를 만들어도 남에게 GRANT 할 때
--     `ORA-01720: grant option does not exist` 가 난다
--   그래서 A 는 DBA 가 해야 한다.
--
-- ## 계정을 왜 둘로 나누는가 — 하나로 합치면 안 되는 이유
-- `SYS_OBJECT_USER` 에는 **LOGONPASSWORD**(로그인 비밀번호)·MOBILE·ADDRESS·BIRTHDATE 가
-- 함께 있다. 뷰를 만들려면 소유자가 그 테이블을 SELECT 할 수 있어야 하므로,
-- **뷰 소유자 계정은 사실상 비밀번호 컬럼에 접근할 수 있다.**
-- 봇이 그 계정으로 붙으면 "필요한 컬럼만 뷰로 노출한다"는 설계가 장식이 된다.
-- 봇 자격증명이 새는 순간 원본 전체가 새기 때문이다.
--
--   TYSLACK      — 스키마 소유자. 뷰를 소유한다. 사람이 유지보수할 때만 쓴다
--   TYSLACK_BOT  — 봇이 쓰는 계정. 접속 + 뷰 2개 SELECT 뿐. 그 외 아무 권한도 없다
--
-- 이름에서 `_RO` 를 뺐다. 역할이 이름에 드러나는 편이 낫다는 판단이면
-- `TYSLACK_BOT` 대신 `TYSLACK_READER` 등으로 바꿔도 된다. **둘로 나누는 것만 지키면 된다.**


-- ===========================================================================
-- A. DBA 계정으로 실행
-- ===========================================================================

-- A-0. 먼저 확인 — 계정이 6개월 뒤 조용히 잠기는 것을 막는다.
--
-- Oracle 12c 의 DEFAULT 프로파일은 `PASSWORD_LIFE_TIME` 이 180일이고
-- `FAILED_LOGIN_ATTEMPTS` 가 10회다. 그대로 두면 **봇이 6개월 뒤 갑자기 접속에 실패한다.**
-- 원인을 찾기 어려운 종류의 장애다.
--
--   SELECT resource_name, limit FROM dba_profiles
--    WHERE profile = 'DEFAULT'
--      AND resource_name IN ('PASSWORD_LIFE_TIME', 'PASSWORD_GRACE_TIME',
--                            'FAILED_LOGIN_ATTEMPTS', 'PASSWORD_VERIFY_FUNCTION');
--
-- `PASSWORD_LIFE_TIME` 이 UNLIMITED 가 아니면 전용 프로파일을 만들어 붙인다.
-- (사내 보안 정책상 만료가 필요하면 만들지 말고, 대신 만료일을 달력에 적어 둔다.)
--
--   CREATE PROFILE TYSLACK_SVC LIMIT
--       PASSWORD_LIFE_TIME UNLIMITED
--       FAILED_LOGIN_ATTEMPTS 10;
--
-- 아래 CREATE USER 에 `PROFILE TYSLACK_SVC` 를 덧붙이면 된다.
--
-- `PASSWORD_VERIFY_FUNCTION` 이 걸려 있으면 비밀번호 복잡도 조건이 있다.
-- 특수문자를 쓸 경우 큰따옴표로 감싼다(아래처럼).

-- A-1. 스키마 소유자. Oracle 에서는 계정 하나가 곧 스키마 하나다.
--
-- 테이블스페이스 이름은 이 DB 에서 확인한 값이다(2026-08-31).
-- `USERS` 는 없다 — 그대로 쓰면 ORA-00959 가 난다.
CREATE USER TYSLACK IDENTIFIED BY "<강한 비밀번호 1>"
    DEFAULT TABLESPACE TS_GW_DATA
    TEMPORARY TABLESPACE TEMP;

-- **테이블스페이스 할당량(QUOTA)을 주지 않는다.**
-- 뷰는 데이터를 저장하지 않으므로 할당량이 필요 없고, 할당량이 없으면
-- 이 계정으로 테이블을 만들어 원본을 복제해 두는 사고도 원천적으로 막힌다.

GRANT CREATE SESSION TO TYSLACK;
GRANT CREATE VIEW    TO TYSLACK;
-- CONNECT·RESOURCE 롤은 주지 않는다. 필요 이상의 권한이 딸려온다.

-- A-2. 원본 테이블 읽기 권한. **WITH GRANT OPTION 이 반드시 필요하다.**
-- 이게 없으면 B-3 의 GRANT 에서 ORA-01720 이 난다.
GRANT SELECT ON COVI_SMART4J.SYS_OBJECT_GROUP TO TYSLACK WITH GRANT OPTION;
GRANT SELECT ON COVI_SMART4J.SYS_OBJECT_USER  TO TYSLACK WITH GRANT OPTION;

-- A-3. 봇이 쓸 계정. 접속 권한만 준다. 나머지는 B-3 에서 뷰에만 붙인다.
CREATE USER TYSLACK_BOT IDENTIFIED BY "<강한 비밀번호 2>"
    DEFAULT TABLESPACE TS_GW_DATA
    TEMPORARY TABLESPACE TEMP;

GRANT CREATE SESSION TO TYSLACK_BOT;
-- 원본 테이블 권한은 **절대 주지 않는다.** LOGONPASSWORD 가 그 안에 있다.

-- 두 계정 모두 **테이블스페이스 할당량을 주지 않는다.** 뷰는 저장하는 게 없어 필요 없고,
-- 할당량이 없으면 이 계정으로 원본을 테이블에 복제해 두는 사고가 원천 차단된다.
--   → `ALTER USER ... QUOTA ... ON ...` 를 실행하지 않으면 된다. 기본값이 0이다.

-- 두 비밀번호는 채팅·메일·이슈에 붙여넣지 않는다. 서버 설정 파일에만 넣는다.


-- ===========================================================================
-- B. TYSLACK 계정으로 실행
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- B-1. 조직 트리
-- ---------------------------------------------------------------------------
-- 필터
--   GROUPTYPE='Dept'      — Division/JobTitle/JobLevel/Company/Community/Authority 제외.
--                           그 값들은 부서가 아니라 직급·직책·게시판 그룹이다.
--   GROUPPATH IS NOT NULL — 경로가 없는 1건(testdept10)은 시험 데이터다.
--
-- **미사용 부서도 함께 내보낸다.** 지우지 않고 active=false 로 끄기 위해서다.
-- 지워 버리면 과거 대화에 나오는 조직코드를 나중에 해석할 수 없다.
--
-- parent_org_code
--   MEMBEROF 를 그대로 쓰되, 그 코드가 이 뷰 안에 없으면(회사/부문 노드를 가리키는 경우)
--   NULL 로 만든다. 없는 부모를 가리키면 받는 쪽에서 외래키가 깨진다.
--   그래서 각 회사의 최상위 부서들이 뿌리가 된다 — 단일 루트가 아니다(2026-08-31 기준 149개).
--
-- org_kind — **이름 기준 추정이다. 정답이 아니다.**
--   그룹웨어에 본부/팀/현장을 구분하는 컬럼이 없어 이름과 코드 모양으로 나눈다.
--     숫자 코드              → site.  단 이름에 TFT 가 있으면 project
--     이름이 본부/실/부문     → hq
--     이름이 (주)/㈜ 로 끝남  → project (SPC 법인)
--     그 외                   → team
--   2026-08-31 결과: hq 12 · team 134 · site 157 · project 20 (사용중 기준).
--   `경영진`·`이사회의장` 같은 것은 team 으로 떨어진다. 애매한 것은 team 이다.
--   틀려도 **권한 판정은 parent 트리로 하므로 안전하다.** kind 는 표시·분류용이다.

CREATE OR REPLACE VIEW V_TYSLACK_ORG AS
SELECT g.GROUPCODE                                        AS org_code,
       g.DISPLAYNAME                                      AS org_name,
       CASE
           WHEN EXISTS (SELECT 1
                          FROM COVI_SMART4J.SYS_OBJECT_GROUP p
                         WHERE p.GROUPCODE = g.MEMBEROF
                           AND p.GROUPTYPE = 'Dept'
                           AND p.GROUPPATH IS NOT NULL)
           THEN g.MEMBEROF
       END                                                AS parent_org_code,
       CASE
           WHEN REGEXP_LIKE(g.GROUPCODE, '^[0-9]+$')
               THEN CASE WHEN g.DISPLAYNAME LIKE '%TFT%'
                         THEN 'project' ELSE 'site' END
           WHEN g.DISPLAYNAME LIKE '%본부'
             OR g.DISPLAYNAME LIKE '%실'
             OR g.DISPLAYNAME LIKE '%부문'   THEN 'hq'
           WHEN g.DISPLAYNAME LIKE '%(주)'
             OR g.DISPLAYNAME LIKE '%㈜'     THEN 'project'
           ELSE 'team'
       END                                                AS org_kind,
       -- 계열사 경계. TY(태영건설)와 SUB/SPC(자회사·SPC)를 섞으면 안 된다.
       g.COMPANYCODE                                      AS company_code,
       g.GROUPPATH                                        AS org_path,
       g.ISUSE                                            AS use_yn
  FROM COVI_SMART4J.SYS_OBJECT_GROUP g
 WHERE g.GROUPTYPE = 'Dept'
   AND g.GROUPPATH IS NOT NULL;

-- ---------------------------------------------------------------------------
-- B-2. 인사 기본
-- ---------------------------------------------------------------------------
-- 필터
--   ISHR='Y' — 인사연동 대상만. 이걸로 관리자·시스템·외부 계정이 빠진다.
--              재직 1299명 중 ISHR='Y' 는 1120명. 나머지 179는 시스템/외부 계정이고,
--              USERCODE 와 EMPNO 가 어긋나는 3건도 전부 거기에 있다.
--
-- **퇴직자는 ISHR 이 'N' 으로 바뀐다.** 미사용 3068명 중 ISHR='Y' 는 9명뿐이다.
-- 즉 퇴직자는 이 뷰에서 **사라진다.** 받는 쪽 반영 잡은
-- "이번 스냅샷에 없는 사번 = active=false" 로 처리해야 한다.
-- 안 그러면 퇴직자가 계속 조회 권한을 갖는다.
--
-- emp_no 는 USERCODE 를 쓴다. 다른 테이블이 이 값을 키로 참조한다.
-- 이메일은 소문자로 내보낸다 — Slack 프로필과 맞출 때 대소문자가 어긋나면
-- 같은 사람을 다른 사람으로 잡는다. 확인 결과 중복은 0이다.
--
-- 내보내지 않는 것: LOGONPASSWORD · MOBILE · PHONENUMBER · ADDRESS · BIRTHDATE ·
--                   PHOTOPATH · ENTERDATE · RETIREDATE

CREATE OR REPLACE VIEW V_TYSLACK_EMP AS
SELECT u.USERCODE                          AS emp_no,
       -- NICKNAME(실명)이 비어 있는 사람이 있다(2026-08-31 기준 재직자 3명).
       -- 받는 쪽 employee.name 은 NOT NULL 이라 그대로 두면 반영이 통째로 실패한다.
       -- DISPLAYNAME 은 원본에서 NOT NULL 이므로 이걸로 받친다.
       COALESCE(u.NICKNAME, u.DISPLAYNAME)  AS emp_name,
       LOWER(u.MAILADDRESS)                AS email,
       u.DEPARTMENTMANAGECODE              AS org_code,
       u.JOBDUTY                           AS position_name,
       u.ISUSE                             AS use_yn
  FROM COVI_SMART4J.SYS_OBJECT_USER u
 WHERE u.ISHR = 'Y';

-- ---------------------------------------------------------------------------
-- B-3. 봇 계정에 뷰만 열어 준다
-- ---------------------------------------------------------------------------
GRANT SELECT ON V_TYSLACK_ORG TO TYSLACK_BOT;
GRANT SELECT ON V_TYSLACK_EMP TO TYSLACK_BOT;

-- 봇이 스키마 이름 없이 부를 수 있게 하려면(선택) DBA 가 공용 시노님을 만든다:
--   CREATE PUBLIC SYNONYM V_TYSLACK_ORG FOR TYSLACK.V_TYSLACK_ORG;
--   CREATE PUBLIC SYNONYM V_TYSLACK_EMP FOR TYSLACK.V_TYSLACK_EMP;
-- 안 만들어도 된다. 우리 쪽 추출 SQL 은 TYSLACK. 접두사를 붙여 부른다.


-- ===========================================================================
-- C. 확인
-- ===========================================================================

-- C-1. 봇 계정 권한이 딱 이것뿐인지 (DBA 계정으로)
--   SELECT * FROM dba_sys_privs  WHERE grantee = 'TYSLACK_BOT';   -- CREATE SESSION 하나
--   SELECT * FROM dba_tab_privs  WHERE grantee = 'TYSLACK_BOT';   -- 뷰 2개 SELECT 뿐
--   SELECT granted_role FROM dba_role_privs WHERE grantee = 'TYSLACK_BOT';  -- 없어야 한다

-- C-2. 봇 계정으로 접속해 원본이 안 보이는지 — **에러가 나야 정상이다**
--   SELECT count(*) FROM COVI_SMART4J.SYS_OBJECT_USER;   -- ORA-00942 여야 한다
--   SELECT count(*) FROM TYSLACK.V_TYSLACK_EMP;          -- 이건 되어야 한다

-- C-3. 데이터가 예상과 맞는지 (2026-08-31 기준 값)
--   SELECT use_yn, org_kind, count(*) FROM TYSLACK.V_TYSLACK_ORG
--    GROUP BY use_yn, org_kind ORDER BY 1, 3 DESC;
--     → Y: site 157 · team 134 · project 20 · hq 12
--   SELECT company_code, count(*) FROM TYSLACK.V_TYSLACK_ORG WHERE use_yn='Y'
--    GROUP BY company_code ORDER BY 2 DESC;   → TY 241 이 최상위
--   SELECT use_yn, count(*), count(email), count(org_code) FROM TYSLACK.V_TYSLACK_EMP
--    GROUP BY use_yn;                          → Y: 1120 / 1076 / 1113

-- C-4. 트리가 성한지 — 넷 다 0 이어야 한다
--   부모가 뷰에 없는 조직:
--     SELECT count(*) FROM TYSLACK.V_TYSLACK_ORG c
--      WHERE c.parent_org_code IS NOT NULL
--        AND NOT EXISTS (SELECT 1 FROM TYSLACK.V_TYSLACK_ORG p
--                         WHERE p.org_code = c.parent_org_code);
--   자기 자신이 부모:
--     SELECT count(*) FROM TYSLACK.V_TYSLACK_ORG WHERE org_code = parent_org_code;
--   소속 조직이 뷰에 없는 직원:
--     SELECT count(*) FROM TYSLACK.V_TYSLACK_EMP e
--      WHERE e.org_code IS NOT NULL
--        AND NOT EXISTS (SELECT 1 FROM TYSLACK.V_TYSLACK_ORG o
--                         WHERE o.org_code = e.org_code);
--   순환 참조:
--     SELECT count(*) FROM TYSLACK.V_TYSLACK_ORG
--      WHERE CONNECT_BY_ISCYCLE = 1
--      START WITH parent_org_code IS NULL
--    CONNECT BY NOCYCLE PRIOR org_code = parent_org_code;
