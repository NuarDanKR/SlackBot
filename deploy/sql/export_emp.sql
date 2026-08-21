-- 인사기본 스냅샷 → JSONL
-- 실행: sqlplus -s TYBOT_RO/pw@ORCL @export_emp.sql > emp.jsonl
-- 주의: 뷰에 주민번호·연락처·급여 컬럼이 없어야 한다(V_TYBOT_EMP 정의 확인).
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
  FROM V_TYBOT_EMP
 ORDER BY emp_no;

EXIT SUCCESS
