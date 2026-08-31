-- !! 우리 DB(12.1)에서는 쓸 수 없다. `JSON_OBJECT` 는 12.2 에서 추가됐다.
--    ORA-00904 로 실패한다. 대신 `export_emp_12_1.sql` 을 쓴다.
--    이 파일은 12.2 이상으로 올라갔을 때를 위해 남겨 둔다.

-- 인사기본 스냅샷 → JSONL
-- 실행: sqlplus -s TYSLACK_BOT/pw@ORCL @export_emp.sql > emp.jsonl
-- 주의: 뷰에 주민번호·연락처·급여 컬럼이 없어야 한다(V_TYSLACK_EMP 정의 확인).
SET PAGESIZE 0
SET FEEDBACK OFF
SET HEADING OFF
SET ECHO OFF
SET VERIFY OFF
SET TERMOUT OFF
SET TRIMSPOOL ON
SET TRIMOUT ON
SET LINESIZE 32767
SET LONG 100000000
SET NEWPAGE NONE
SET SQLBLANKLINES ON
WHENEVER SQLERROR EXIT FAILURE
WHENEVER OSERROR EXIT FAILURE

SELECT JSON_OBJECT(
         KEY 'emp_no'   VALUE emp_no,
         KEY 'name'     VALUE emp_name,
         KEY 'email'    VALUE LOWER(email),
         KEY 'org_code' VALUE org_code,
         KEY 'position' VALUE position_name,
         KEY 'active'   VALUE CASE WHEN use_yn = 'Y' THEN 'true' ELSE 'false' END FORMAT JSON
       )
  FROM V_TYSLACK_EMP
 ORDER BY emp_no;

EXIT SUCCESS
